python train_end2end_isic_224.py \
    --model pix2pix  \
    --is_train True \
    --cuda True \
    --gpu_ids 1 \
    --cuda_index 1 \
    --dataroot ../data/ISIC2018 \
    --amp \
    --loss_lambda 1.0 \
    --n_epochs 5000 \
    --unet_epoch 20 \
    --lr 2e-6 \
    --arch_lr 1e-6 \
    --lr_d_dcgan 0 \
    --display_freq 10 \
    --classes 1 \
    --output_nc 3 \
    --input_nc 1 \
    --batch_size 2 \
    --seg_model vit \
    --unet_learning_rate 2.5e-4