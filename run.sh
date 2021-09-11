export DISCRETE_DATA_DIR=/home/yuerxin/discrete-text-representation/cls_data_dir
export DISCRETE_PROJECT_DIR=/home/yuerxin/discrete-text-representation


# CUDA_VISIBLE_DEVICES=2 python main.py \
# -c config/base.conf \
# -o 'expname=demo, runname=ag_word_vq,
# quantizer.level=word, quantizer.M=1, quantizer.K=1024, quantizer.type=vq, vq.commitment_cost=1e-3, vq.use_ema=0,
# phase=pretrain, pretrain.use_noam=0, ckpt_path=none'
 


CUDA_VISIBLE_DEVICES=2 python main.py \
 -c config/base.conf \
 -o 'expname=demo, runname=ag_word_vq,
 quantizer.level=word, quantizer.M=1, quantizer.K=1024, quantizer.type=vq, vq.commitment_cost=1e-3, vq.use_ema=0,
 phase=target_train, target=${target-tmpl}${target-200-tmpl}{test=0}, sub_runname=cls200, ckpt_path=current'