#!/usr/bin/python
# -*- encoding: utf-8 -*-


from logger import setup_logger
from model import BiSeNet
from cityscapes import CityScapes
from loss import OhemCELoss
from evaluate import evaluate
from optimizer import Optimizer

import torch
import torch.nn as nn
from torch.utils.data import DataLoader
import torch.nn.functional as F
import torch.distributed as dist

import os
import os.path as osp
import logging
import time
import datetime
import argparse
from torchsummary import summary
from pytorch_jacinto_ai import xnn

respth = './res'
if not osp.exists(respth): os.makedirs(respth)
logger = logging.getLogger()


def parse_args():
    parse = argparse.ArgumentParser()
    parse.add_argument(
            '--local_rank',
            dest = 'local_rank',
            type = int,
            default = -1,
            )
    return parse.parse_args()

def store_stats(net=None, write_onnx = False, write_torch_script = True):
    sample_batch_size, channel, height, width = [8, 3, 360, 640]
    dummy_input = torch.randn(sample_batch_size, channel, height, width, device='cuda')
    flops = xnn.utils.forward_count_flops(net, dummy_input)
    print("GFlops : {:.2f} GMAC : {:.2f} ".format(flops/1E9, (flops/(2*1E9))))

    version = torch.__version__.split('.')
    print(version)
    torch_version = 100*int(version[0]) + 10*int(version[1]) + int(version[2])
    print("torch_version: ", torch_version)
    if write_torch_script and torch_version > 110:
        # Use torch.jit.trace to generate a torch.jit.ScriptModule via tracing.
        traced_script_module = torch.jit.trace(net, dummy_input)
        pt_file_name = os.path.join('/data/files/work/temp/BiSe', "BiSeNet_diss.pth")
        print("saving ", pt_file_name)
        torch.jit.save(traced_script_module, pt_file_name)
        exit("ABN is not supported in PyTorch 1.4/1.5 version exiting!!! ")

    if write_onnx:   
        onnx_file_name = os.path.join('/data/files/work/temp/BiSe', "BiSeNet_diss.onnx")
        print(onnx_file_name)
        print(summary(net, dummy_input.shape[1:]))
        torch.onnx.export(net, dummy_input, onnx_file_name, do_constant_folding=True, opset_version=10)

def train():
    args = parse_args()
    torch.cuda.set_device(args.local_rank)
    dist.init_process_group(
                backend = 'nccl',
                init_method = 'tcp://127.0.0.1:33271',
                world_size = torch.cuda.device_count(),
                rank=args.local_rank
                )
    setup_logger(respth)

    ## dataset
    n_classes = 19
    n_img_per_gpu = 8
    n_workers = 4
    cropsize = [1024, 1024]
    ds = CityScapes('./data', cropsize=cropsize, mode='train')
    sampler = torch.utils.data.distributed.DistributedSampler(ds)
    dl = DataLoader(ds,
                    batch_size = n_img_per_gpu,
                    shuffle = False,
                    sampler = sampler,
                    num_workers = n_workers,
                    pin_memory = True,
                    drop_last = True)

    ## model
    ignore_idx = 255
    net = BiSeNet(n_classes=n_classes)
    net.cuda()

    store_stats(net=net)

    net.train()
    net = nn.parallel.DistributedDataParallel(net,
            device_ids = [args.local_rank, ],
            output_device = args.local_rank
            )
    score_thres = 0.7
    n_min = n_img_per_gpu*cropsize[0]*cropsize[1]//16
    criteria_p = OhemCELoss(thresh=score_thres, n_min=n_min, ignore_lb=ignore_idx)
    criteria_16 = OhemCELoss(thresh=score_thres, n_min=n_min, ignore_lb=ignore_idx)
    criteria_32 = OhemCELoss(thresh=score_thres, n_min=n_min, ignore_lb=ignore_idx)

    ## optimizer
    momentum = 0.9
    weight_decay = 5e-4
    lr_start = 1e-2
    max_iter = 80000
    power = 0.9
    warmup_steps = 1000
    warmup_start_lr = 1e-5
    optim = Optimizer(
            model = net.module,
            lr0 = lr_start,
            momentum = momentum,
            wd = weight_decay,
            warmup_steps = warmup_steps,
            warmup_start_lr = warmup_start_lr,
            max_iter = max_iter,
            power = power)

    ## train loop
    msg_iter = 50
    loss_avg = []
    st = glob_st = time.time()
    diter = iter(dl)
    epoch = 0
    for it in range(max_iter):
        try:
            im, lb = next(diter)
            if not im.size()[0]==n_img_per_gpu: raise StopIteration
        except StopIteration:
            epoch += 1
            sampler.set_epoch(epoch)
            diter = iter(dl)
            im, lb = next(diter)
        im = im.cuda()
        lb = lb.cuda()
        H, W = im.size()[2:]
        lb = torch.squeeze(lb, 1)

        optim.zero_grad()
        out, out16, out32 = net(im)
        lossp = criteria_p(out, lb)
        loss2 = criteria_16(out16, lb)
        loss3 = criteria_32(out32, lb)
        loss = lossp + loss2 + loss3
        loss.backward()
        optim.step()

        loss_avg.append(loss.item())
        ## print training log message
        if (it+1)%msg_iter==0:
            loss_avg = sum(loss_avg) / len(loss_avg)
            lr = optim.lr
            ed = time.time()
            t_intv, glob_t_intv = ed - st, ed - glob_st
            eta = int((max_iter - it) * (glob_t_intv / it))
            eta = str(datetime.timedelta(seconds=eta))
            msg = ', '.join([
                    'it: {it}/{max_it}',
                    'lr: {lr:4f}',
                    'loss: {loss:.4f}',
                    'eta: {eta}',
                    'time: {time:.4f}',
                ]).format(
                    it = it+1,
                    max_it = max_iter,
                    lr = lr,
                    loss = loss_avg,
                    time = t_intv,
                    eta = eta
                )
            logger.info(msg)
            loss_avg = []
            st = ed

    ## dump the final model
    save_pth = osp.join(respth, 'model_final.pth')
    net.cpu()
    state = net.module.state_dict() if hasattr(net, 'module') else net.state_dict()
    if dist.get_rank()==0: torch.save(state, save_pth)
    logger.info('training done, model saved to: {}'.format(save_pth))


if __name__ == "__main__":
    train()
    evaluate()
