import os
import sys
import time
import shutil
import wandb
import logging
import imgaug as ia
import numpy as np
from PIL import Image
from imgaug import augmenters as iaa

import torch
import torchvision
import torch.nn as nn
from torch import optim
import torch.nn.functional as F
from torch.autograd import Variable
import torchvision.transforms as transforms
from torch.utils.data import DataLoader, random_split, Subset

from util import util
# from util.data_loading import BasicDataset
from util.SE_loader import CarvanaDataset as BasicDataset
from util.dice_score import dice_loss
from options.train_options import TrainOptions

from models_pix2pix import create_model
from models_pix2pix import networks
from unet import UNet
from deeplab import *

from betty.engine import Engine
from betty.configs import Config, EngineConfig
from betty.problems import ImplicitProblem

def jaccard_index(y_true, y_pred, smooth=1):
    if y_pred.dim() != 2:
        jac = 0.0
        for i in range(y_pred.size()[0]):
            intersection = torch.abs(y_true[i] * y_pred[i]).sum(dim=(-1, -2))
            sum_ = torch.sum(torch.abs(y_true[i]) + torch.abs(y_pred[i]), dim=(-1, -2))
            jac += (intersection + smooth) / (sum_ - intersection + smooth)
        jac = jac / y_pred.size()[0]
    else:
        intersection = torch.abs(y_true * y_pred).sum(dim=(-1, -2))
        sum_ = torch.sum(torch.abs(y_true) + torch.abs(y_pred), dim=(-1, -2))
        jac = (intersection + smooth) / (sum_ - intersection + smooth)
    #return (1 - jac) * smooth
    return jac

def jaccard_index_loss(y_true, y_pred, smooth=1):
    return (1 - jaccard_index(y_true, y_pred)) * smooth

def evaluate(net, dataloader, device, amp):
    net.eval()
    num_val_batches = len(dataloader)
    JC_index = 0

    # iterate over the validation set
    with torch.no_grad():
        # for batch in tqdm(dataloader, total=num_val_batches, desc='Validation round', unit='batch', leave=False):
        for i, batch in enumerate(dataloader):
            image, mask_true = batch['image'], batch['mask']

            # move images and labels to correct device and type
            image = image.to(device=device, dtype=torch.float32, memory_format=torch.channels_last)
            # mask_true = mask_true.to(device=device, dtype=torch.long)
            mask_true = mask_true.to(device=device, dtype=torch.long).squeeze(0)
            
            # predict the mask
            mask_pred = net(image)

            assert mask_true.min() >= 0 and mask_true.max() <= 1, 'True mask indices should be in [0, 1]'
            mask_pred = (torch.sigmoid(mask_pred) > 0.5).float()
            # compute the Dice score
            JC_index += jaccard_index(mask_pred.squeeze(), mask_true.squeeze())

    net.train()
    return JC_index / max(num_val_batches, 1)


opt = TrainOptions().parse()   # get training options
assert opt.cuda_index == int(opt.gpu_ids[0]), 'gpu types should be same'
device = torch.device('cuda:0' if opt.cuda_index == 0 else 'cuda:1')
save_path = './checkpoint_se/'+'UNet_100-Sepa-'+time.strftime("%Y%m%d-%H%M%S")
if not os.path.exists(save_path):
    os.mkdir(save_path) 
unet_save_path = save_path+'/unet.pkl'  

##### Initialize logging #####
# logger = wandb.init(project='end2end-unet-ISIC', name="unet-200", resume='allow', anonymous='must')
logger = wandb.init(project='SE-cell', name="SE-100-Sepa", resume='allow', anonymous='must')
logger.config.update(vars(opt))
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

##### create models: pix2pix, unet #####
model = create_model(opt)      # create a model given opt.model and other options
model.setup(opt)               # regular setup: load and print networks; create schedulers
# load pre-trained model
model_path = './pix2pix_se_model/20230410-072252-pix2pix-UNet-100'
model.load_model(model_path + '/pix2pix_discriminator.pkl', model_path + '/pix2pix_generator.pkl')

net = UNet(n_channels=opt.output_nc, n_classes=opt.classes, bilinear=opt.bilinear)
net = net.to(device=device)

##### define optimizer for unet #####
optimizer_unet = optim.RMSprop(net.parameters(), lr=opt.unet_learning_rate, 
                                weight_decay=1e-8, momentum=0.9, foreach=True)
scheduler_unet = optim.lr_scheduler.ReduceLROnPlateau(optimizer_unet, 'max', patience=2)  # goal: maximize Dice score
# scheduler_unet = optim.lr_scheduler.CosineAnnealingLR(optimizer_unet, T_max=500, eta_min=1e-9)
grad_scaler = torch.cuda.amp.GradScaler(enabled=opt.amp)

# Image Augmenter
seq = iaa.Sequential([
    iaa.Fliplr(0.5), # horizontal flips
    # iaa.Flipud(0.25), # vertical flips

    iaa.CropAndPad(percent=(0, 0.1)), # random crops and pad

    # Apply affine transformations to each image.
    # Scale/zoom them, translate/move them, rotate them and shear them.        
    iaa.Affine(scale={"x": (0.8, 1.2), "y": (0.8, 1.2)},),
    iaa.Affine(translate_percent={"x": (-0.1, 0.1), "y": (-0.1, 0.1)},),
    iaa.Affine(rotate=(-15, 15),),
    iaa.Affine(shear=(-8, 8),)
], random_order=True) # apply augmenters in random order

##### prepare dataloader #####
dataset = BasicDataset(opt.dataroot+'/images', opt.dataroot+'/labels', 1.0, '')

n_test = 20
n_train = 60
n_val = 20

indices = list(range(len(dataset)))
train_set = Subset(dataset, indices[:n_train])
val_set = Subset(dataset, indices[n_train:n_train+n_val])
test_set = Subset(dataset, indices[-n_test:])

loader_args = dict(batch_size=opt.batch_size, num_workers=4, pin_memory=True)
train_loader = DataLoader(train_set, shuffle=True, drop_last=True, **loader_args)
val_loader = DataLoader(val_set, shuffle=False, drop_last=True, **loader_args)


##### Training of vanilla UNet #####
total_iters = 0                # the total number of training iterations
val_best_score = 0.0          # the best score of unet
criterion = nn.CrossEntropyLoss() if opt.classes > 1 else nn.BCEWithLogitsLoss()

while total_iters < opt.n_epochs:
    for i, batch in enumerate(train_loader):
        images = batch['image'].to(device=device, dtype=torch.float32)
        true_masks = batch['mask'].to(device=device, dtype=torch.long).squeeze(0)
        total_iters += 1

        masks_pred = net(images)
        loss = criterion(masks_pred.squeeze(0), true_masks.float())
        loss += jaccard_index_loss(torch.sigmoid(masks_pred.squeeze()), true_masks.float().squeeze())

        # fake images and masks
        fake_mask = true_masks.to('cpu', torch.float).numpy()
        if opt.batch_size > 1:
            for i in range(opt.batch_size):
                fake_mask[i] = seq(images=fake_mask[i])
        else:
            fake_mask =seg(images=fake_mask)
        fake_mask = torch.tensor(fake_mask).type(torch.cuda.FloatTensor).to(device=device)
        zero = torch.zeros_like(fake_mask)
        one = torch.ones_like(fake_mask)
        fake_mask = torch.where(fake_mask > 0.1, one, zero)
        fake_image = model.netG(fake_mask)

        fake_mask = fake_mask.squeeze(0)
        fake_image = ((fake_image-fake_image.min()) / (fake_image.max()-fake_image.min())).detach()
        # fake_image = fake_trans(fake_image.mul(255).add_(0.5).clamp_(0, 255).to(torch.uint8)) / 255.0

        fake_pred = net(fake_image)
        fake_loss = criterion(fake_pred, fake_mask.float())
        fake_loss += jaccard_index_loss(torch.sigmoid(fake_pred.squeeze()), fake_mask.float().squeeze())

        loss = loss + 1.0 * fake_loss

        optimizer_unet.zero_grad(set_to_none=True)
        grad_scaler.scale(loss).backward()
        grad_scaler.step(optimizer_unet)
        grad_scaler.update()

        if total_iters % opt.display_freq == 0:
            val_score = evaluate(net, val_loader, device, opt.amp)
            if val_score > val_best_score:
                val_best_score = val_score
                torch.save(net.state_dict(), unet_save_path)
            message = 'Iters: %s, Performance of UNet: ' % (total_iters)
            message += '%s: %.5f, %s: %.5f' % ('loss', loss, 'val', val_score)
            logging.info(message)
            logger.log({'val_score': val_score})

            ims_dict = {}
            img = wandb.Image((fake_mask[0].squeeze().to('cpu').numpy()*255).astype(np.uint8))
            ims_dict['fake_mask'] = img
            img = wandb.Image((fake_image[0].squeeze().to('cpu').numpy()*255).transpose((1, 2, 0)).astype(np.uint8))
            ims_dict['fake_image'] = img
            img = wandb.Image((true_masks[0].squeeze().to('cpu').numpy()*255).astype(np.uint8))
            ims_dict['true_mask'] = img
            img = wandb.Image((images[0].squeeze().to('cpu').numpy()*255).transpose((1, 2, 0)).astype(np.uint8))
            ims_dict['true_image'] = img
            logger.log(ims_dict)
    
    scheduler_unet.step(val_best_score)
torch.save(net.state_dict(), save_path+'/final.pkl')
    
