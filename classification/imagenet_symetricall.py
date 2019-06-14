from __future__ import print_function
import sys

import argparse
import os
import shutil
import time
import random
import numpy as np

import torch
import torch.nn as nn
import torch.nn.parallel
import torch.backends.cudnn as cudnn
import torch.optim as optim
import torch.utils.data as data
import torchvision.transforms as transforms
import torchvision.transforms.functional as F
import torchvision.datasets as datasets
import torchvision.models as models
import models.imagenet as customized_models
from flops_counter import get_model_complexity_info
from PIL import ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True

from utils import Bar, Logger, AverageMeter, accuracy, mkdir_p

# for servers to immediately record the logs
def flush_print(func):
    def new_print(*args, **kwargs):
        func(*args, **kwargs)
        sys.stdout.flush()
    return new_print
print = flush_print(print)

class SMSELoss(nn.MSELoss):
    def __init__(self, batch_size = 256, W=7, reduction='mean'):
        super(SMSELoss, self).__init__(None, None, reduction)
        self.indices  = torch.tensor(range(W - 1, -1, -1)).cuda()
        self.odd_inds = torch.tensor(range(1, batch_size, 2)).cuda()
        self.even_inds= torch.tensor(range(0, batch_size, 2)).cuda()
        print(self.indices, self.odd_inds, self.even_inds)

    def forward(self, input):
        #print('input size = ', input.size())
        left  = input.index_select(0, self.even_inds)
        right = input.index_select(0, self.odd_inds)
        #print('left size = ', left.size(), 'right size = ', right.size())
        right = torch.index_select(right, 3, self.indices)
        #print('left size = ', left.size(), 'right size = ', right.size())
        return torch.nn.functional.mse_loss(left, right, reduction=self.reduction)

# Models
default_model_names = sorted(name for name in models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(models.__dict__[name]))

customized_models_names = sorted(name for name in customized_models.__dict__
    if name.islower() and not name.startswith("__")
    and callable(customized_models.__dict__[name]))

for name in customized_models.__dict__:
    if name.islower() and not name.startswith("__") and callable(customized_models.__dict__[name]):
        models.__dict__[name] = customized_models.__dict__[name]

model_names = default_model_names + customized_models_names

# Parse arguments
parser = argparse.ArgumentParser(description='PyTorch ImageNet Training')

# Datasets
parser.add_argument('-d', '--data', default='path to dataset', type=str)
parser.add_argument('-j', '--workers', default=32, type=int, metavar='N',
                    help='number of data loading workers (default: 4)')
# Optimization options
parser.add_argument('--epochs', default=90, type=int, metavar='N',
                    help='number of total epochs to run')
parser.add_argument('--start-epoch', default=0, type=int, metavar='N',
                    help='manual epoch number (useful on restarts)')
parser.add_argument('--train-batch', default=256, type=int, metavar='N',
                    help='train batchsize (default: 256)')
parser.add_argument('--test-batch', default=200, type=int, metavar='N',
                    help='test batchsize (default: 200)')
parser.add_argument('--lr', '--learning-rate', default=0.1, type=float,
                    metavar='LR', help='initial learning rate')
parser.add_argument('--drop', '--dropout', default=0, type=float,
                    metavar='Dropout', help='Dropout ratio')
parser.add_argument('--schedule', type=int, nargs='+', default=[150, 225],
                        help='Decrease learning rate at these epochs.')
parser.add_argument('--gamma', type=float, default=0.1, help='LR is multiplied by gamma on schedule.')
parser.add_argument('--momentum', default=0.9, type=float, metavar='M',
                    help='momentum')
parser.add_argument('--weight-decay', '--wd', default=1e-4, type=float,
                    metavar='W', help='weight decay (default: 1e-4)')
# Checkpoints
parser.add_argument('-c', '--checkpoint', default='checkpoint', type=str, metavar='PATH',
                    help='path to save checkpoint (default: checkpoint)')
parser.add_argument('--resume', default='', type=str, metavar='PATH',
                    help='path to latest checkpoint (default: none)')
# Architecture
parser.add_argument('--modelsize', '-ms', metavar='large', default='large', \
                    choices=['large', 'small'], \
                    help = 'model_size affects the data augmentation, please choose:' + \
                           ' large or small ')
parser.add_argument('--arch', '-a', metavar='ARCH', default='resnet18',
                    choices=model_names,
                    help='model architecture: ' +
                        ' | '.join(model_names) +
                        ' (default: resnet18)')
parser.add_argument('--depth', type=int, default=29, help='Model depth.')
parser.add_argument('--cardinality', type=int, default=32, help='ResNet cardinality (group).')
parser.add_argument('--base-width', type=int, default=4, help='ResNet base width.')
parser.add_argument('--widen-factor', type=int, default=4, help='Widen factor. 4 -> 64, 8 -> 128, ...')
# Miscs
parser.add_argument('--manualSeed', type=int, help='manual seed')
parser.add_argument('-e', '--evaluate', dest='evaluate', action='store_true',
                    help='evaluate model on validation set')
parser.add_argument('--pretrained', dest='pretrained', action='store_true',
                    help='use pre-trained model')
#Device options
parser.add_argument('--gpu-id', default='0', type=str,
                    help='id(s) for CUDA_VISIBLE_DEVICES')

# Loss
parser.add_argument('--lambda-mse', '-lmse', default=1e-5, type=float,
                    help='loss for mse (default: 1e-5)')


args = parser.parse_args()
state = {k: v for k, v in args._get_kwargs()}

# Use CUDA
os.environ['CUDA_VISIBLE_DEVICES'] = args.gpu_id

# Random seed
if args.manualSeed is None:
    args.manualSeed = random.randint(1, 10000)
random.seed(args.manualSeed)
torch.manual_seed(args.manualSeed)
torch.cuda.manual_seed_all(args.manualSeed)

best_acc = 0  # best test accuracy

def fast_collate(batch):
    imgs = [img[0] for img in batch]
    targets = torch.tensor([target[1] for target in batch], dtype=torch.int64)
    w = imgs[0].size[0]
    h = imgs[0].size[1]
    tensor = torch.zeros((len(imgs), 3, h, w), dtype=torch.uint8)
    for i, img in enumerate(imgs):
        nump_array = np.asarray(img, dtype=np.uint8)
        # tens = torch.from_numpy(nump_array)
        if nump_array.ndim < 3:
            nump_array = np.expand_dims(nump_array, axis=-1)
        nump_array = np.rollaxis(nump_array, 2)
        tensor[i] += torch.from_numpy(nump_array)

    return tensor, targets

def symetric_collate(batch):
    imgs = [img[0] for img in batch] + [F.hflip(img[0]) for img in batch]
    tars = [target[1] for target in batch] + [target[1] for target in batch]
    targets = torch.tensor(tars, dtype=torch.int64)
    w = imgs[0].size[0]
    h = imgs[0].size[1]
    l = len(imgs) // 2
    tensor = torch.zeros((len(imgs), 3, h, w), dtype=torch.uint8)
    for i, img in enumerate(imgs):
        nump_array = np.asarray(img, dtype=np.uint8)
        # tens = torch.from_numpy(nump_array)
        if nump_array.ndim < 3:
            nump_array = np.expand_dims(nump_array, axis=-1)
        nump_array = np.rollaxis(nump_array, 2)
        tensor[i] += torch.from_numpy(nump_array)

    tensor  = tensor.view(2, l, 3, h, w).transpose(0, 1).reshape(-1, 3, h, w)
    targets = targets.view(2, l).transpose(0, 1).reshape(-1)

    return tensor, targets


class data_prefetcher():
    def __init__(self, loader):
        self.loader = iter(loader)
        self.stream = torch.cuda.Stream()
        self.mean   = torch.tensor([0.485 * 255, 0.456 * 255, 0.406 * 255]) \
                .cuda().view(1, 3, 1, 1)
        self.std    = torch.tensor([0.229 * 255, 0.224 * 255, 0.225 * 255]) \
                .cuda().view(1, 3, 1, 1)
        self.preload()

    def preload(self):
        try:
            self.next_input, self.next_target = next(self.loader)
        except StopIteration:
            self.next_input = None
            self.next_target = None
            return
        
        with torch.cuda.stream(self.stream):
            self.next_input = self.next_input.cuda(non_blocking=True)
            self.next_target = self.next_target.cuda(non_blocking=True)
            self.next_input = self.next_input.float()
            self.next_input = self.next_input.sub_(self.mean).div_(self.std)

    def next(self):
        torch.cuda.current_stream().wait_stream(self.stream)
        input = self.next_input
        target = self.next_target
        if input is not None:
            self.preload()
        return input, target


def main():
    global best_acc
    start_epoch = args.start_epoch  # start from epoch 0 or last checkpoint epoch

    if not os.path.isdir(args.checkpoint):
        mkdir_p(args.checkpoint)

    # Data loading code
    traindir = os.path.join(args.data, 'train')
    valdir = os.path.join(args.data, 'valf')
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    data_aug_scale = (0.08, 1.0) if args.modelsize == 'large' else (0.2, 1.0)

    train_loader = torch.utils.data.DataLoader(
        datasets.ImageFolder(traindir, transforms.Compose([
            transforms.RandomResizedCrop(224, scale = data_aug_scale),
            # transforms.RandomHorizontalFlip(),
            # transforms.ToTensor(),
            # normalize,
        ])),
        batch_size=args.train_batch, shuffle=True,
        num_workers=args.workers, pin_memory=True, collate_fn=symetric_collate)

    val_loader = torch.utils.data.DataLoader(
        datasets.ImageFolder(valdir, transforms.Compose([
            transforms.Scale(256),
            transforms.CenterCrop(224),
            # transforms.ToTensor(),
            # normalize,
        ])),
        batch_size=args.test_batch, shuffle=True,
        num_workers=args.workers, pin_memory=True, collate_fn=fast_collate)

    # create model
    if args.pretrained:
        print("=> using pre-trained model '{}'".format(args.arch))
        model = models.__dict__[args.arch](pretrained=True)
    elif 'resnext' in args.arch:
        model = models.__dict__[args.arch](
                    baseWidth=args.base_width,
                    cardinality=args.cardinality,
                )
    else:
        print("=> creating model '{}'".format(args.arch))
        model = models.__dict__[args.arch]()

    flops, params = get_model_complexity_info(model, (224, 224), as_strings=False, print_per_layer_stat=False)
    print('Flops:  %.3f' % (flops / 1e9))
    print('Params: %.2fM' % (params / 1e6))


    if args.arch.startswith('alexnet') or args.arch.startswith('vgg'):
        model.features = torch.nn.DataParallel(model.features)
        model.cuda()
    else:
        model = torch.nn.DataParallel(model).cuda()

    cudnn.benchmark = True

    # define loss function (criterion) and optimizer
    criterion = nn.CrossEntropyLoss().cuda()
    criterion_mse_train = SMSELoss(args.train_batch * 2).cuda()
    criterions_train = (criterion, criterion_mse_train)
    optimizer = optim.SGD(model.parameters(), lr=args.lr, momentum=args.momentum, weight_decay=args.weight_decay)

    # Resume
    title = 'ImageNet-' + args.arch
    if args.resume:
        # Load checkpoint.
        print('==> Resuming from checkpoint..', args.resume)
        assert os.path.isfile(args.resume), 'Error: no checkpoint directory found!'
        args.checkpoint = os.path.dirname(args.resume)
        checkpoint = torch.load(args.resume)
        best_acc = checkpoint['best_acc']
        start_epoch = checkpoint['epoch']
        # model may have more keys
        t = model.state_dict()
        c = checkpoint['state_dict']
        flag = True 
        for k in t:
            if k not in c:
                print('not in loading dict! fill it', k, t[k])
                c[k] = t[k]
                flag = False
        model.load_state_dict(c)
        if flag:
            print('optimizer load old state')
            optimizer.load_state_dict(checkpoint['optimizer'])
        else:
            print('new optimizer !')
        logger = Logger(os.path.join(args.checkpoint, 'log.txt'), title=title, resume=True)
    else:
        logger = Logger(os.path.join(args.checkpoint, 'log.txt'), title=title)
        logger.set_names(['Learning Rate', 'Train Loss', 'Valid Loss', 'Train Acc.', 'Valid Acc.'])


    if args.evaluate:
        print('\nEvaluation only')
        test_loss, test_acc = test(val_loader, model, criterion, start_epoch, args.lambda_mse)
        print(' Test Loss:  %.8f, Test Acc:  %.2f' % (test_loss, test_acc))
        return

    # Train and val
    for epoch in range(start_epoch, args.epochs):
        adjust_learning_rate(optimizer, epoch)

        print('\nEpoch: [%d | %d] LR: %f' % (epoch + 1, args.epochs, state['lr']))

        train_loss, train_acc = train(train_loader, model, criterions_train, optimizer, epoch, args.lambda_mse)
        test_loss, test_acc = test(val_loader, model, criterion, epoch, args.lambda_mse)

        # append logger file
        logger.append([state['lr'], train_loss, test_loss, train_acc, test_acc])

        # save model
        is_best = test_acc > best_acc
        best_acc = max(test_acc, best_acc)
        save_checkpoint({
                'epoch': epoch + 1,
                'state_dict': model.state_dict(),
                'acc': test_acc,
                'best_acc': best_acc,
                'optimizer' : optimizer.state_dict(),
            }, is_best, checkpoint=args.checkpoint)

    logger.close()

    print('Best acc:')
    print(best_acc)

def train(train_loader, model, criterions, optimizer, epoch, lambda_mse):
    criterion, criterion_mse = criterions

    # switch to train mode
    model.train()
    torch.set_grad_enabled(True)

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    losses_ = AverageMeter()
    losses_mse_ = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()
    end = time.time()

    bar = Bar('Processing', max=len(train_loader))
    show_step = len(train_loader) // 10
    
    prefetcher = data_prefetcher(train_loader)
    inputs, targets = prefetcher.next()

    batch_idx = -1
    while inputs is not None:
    # for batch_idx, (inputs, targets) in enumerate(train_loader):
        batch_idx += 1
        batch_size = inputs.size(0)
        if batch_size != args.train_batch * 2:
            break
        # measure data loading time
        data_time.update(time.time() - end)

        #if use_cuda:
        #    inputs, targets = inputs.cuda(), targets.cuda(async=True)
        #inputs, targets = torch.autograd.Variable(inputs), torch.autograd.Variable(targets)

        # compute output
        outputs, features = model(inputs)
        loss_ = criterion(outputs, targets) 
        loss_mse_ = criterion_mse(features) * lambda_mse
        loss  = loss_ + loss_mse_

        # measure accuracy and record loss
        prec1, prec5 = accuracy(outputs.data, targets.data, topk=(1, 5))
        losses.update(loss.data, inputs.size(0))
        losses_.update(loss_.data, inputs.size(0))
        losses_mse_.update(loss_mse_.data, inputs.size(0))
        top1.update(prec1, inputs.size(0))
        top5.update(prec5, inputs.size(0))

        # compute gradient and do SGD step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # plot progress
        bar.suffix  = '({batch}/{size}) Data: {data:.3f}s | Batch: {bt:.3f}s | Total: {total:} | ETA: {eta:} | Loss: {loss:.4f} ({loss_:.4f}, {loss_mse_:.4f})| top1: {top1: .4f} | top5: {top5: .4f}'.format(
                    batch=batch_idx + 1,
                    size=len(train_loader),
                    data=data_time.val,
                    bt=batch_time.val,
                    total=bar.elapsed_td,
                    eta=bar.eta_td,
                    loss=losses.avg,
                    loss_=losses_.avg,
                    loss_mse_=losses_mse_.avg,
                    top1=top1.avg,
                    top5=top5.avg,
                    )
        if (batch_idx) % show_step == 0:
            print(bar.suffix)
        bar.next()

        inputs, targets = prefetcher.next()

    bar.finish()
    return (losses.avg, top1.avg)

def test(val_loader, model, criterion, epoch, lambda_mse):
    global best_acc

    batch_time = AverageMeter()
    data_time = AverageMeter()
    losses = AverageMeter()
    top1 = AverageMeter()
    top5 = AverageMeter()

    # switch to evaluate mode
    model.eval()
    # torch.set_grad_enabled(False)

    end = time.time()
    bar = Bar('Processing', max=len(val_loader))

    prefetcher = data_prefetcher(val_loader)
    inputs, targets = prefetcher.next()

    batch_idx = -1
    while inputs is not None:
    # for batch_idx, (inputs, targets) in enumerate(val_loader):
        batch_idx += 1
        # measure data loading time
        data_time.update(time.time() - end)

        #if use_cuda:
        #    inputs, targets = inputs.cuda(), targets.cuda()
        #inputs, targets = torch.autograd.Variable(inputs, volatile=True), torch.autograd.Variable(targets)

        # compute output
        with torch.no_grad():
            outputs, features = model(inputs)
            loss  = criterion(outputs, targets) 

        # measure accuracy and record loss
        prec1, prec5 = accuracy(outputs.data, targets.data, topk=(1, 5))
        losses.update(loss.data, inputs.size(0))
        top1.update(prec1, inputs.size(0))
        top5.update(prec5, inputs.size(0))

        # measure elapsed time
        batch_time.update(time.time() - end)
        end = time.time()

        # plot progress
        bar.suffix  = '({batch}/{size}) Data: {data:.3f}s | Batch: {bt:.3f}s | Total: {total:} | ETA: {eta:} | Loss: {loss:.4f} | top1: {top1: .4f} | top5: {top5: .4f}'.format(
                    batch=batch_idx + 1,
                    size=len(val_loader),
                    data=data_time.avg,
                    bt=batch_time.avg,
                    total=bar.elapsed_td,
                    eta=bar.eta_td,
                    loss=losses.avg,
                    top1=top1.avg,
                    top5=top5.avg,
                    )
        bar.next()

        inputs, targets = prefetcher.next()


    print(bar.suffix)
    bar.finish()
    return (losses.avg, top1.avg)

def save_checkpoint(state, is_best, checkpoint='checkpoint', filename='checkpoint.pth.tar'):
    filepath = os.path.join(checkpoint, filename)
    torch.save(state, filepath)
    if is_best:
        shutil.copyfile(filepath, os.path.join(checkpoint, 'model_best.pth.tar'))

def adjust_learning_rate(optimizer, epoch):
    global state
    if epoch in args.schedule:
        state['lr'] *= args.gamma
        for param_group in optimizer.param_groups:
            param_group['lr'] = state['lr']

if __name__ == '__main__':
    main()