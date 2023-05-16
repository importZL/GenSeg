python train_deeplab_jsrt.py \
    --model pix2pix  \
    --is_train True \
    --cuda True \
    --gpu_ids 1 \
    --cuda_index 1 \
    --dataroot ../data/JSRT \
    --amp \
    --loss_lambda 1.0 \
    --n_epochs 5000 \
    --unet_epoch 20 \
    --lr 2e-5 \
    --lr_dcgan 4e-5 \
    --lr_d_dcgan 0 \
    --display_freq 10 \
    --classes 1 \
    --output_nc 3 \
    --input_nc 1 \
    --batch_size 2 \
    --unet_learning_rate 2.5e-4
