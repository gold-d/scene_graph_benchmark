# Copyright (c) Facebook, Inc. and its affiliates. All Rights Reserved.
# Copyright (c) 2021 Microsoft Corporation. Licensed under the MIT license. 
r"""
Basic training script for PyTorch
"""

# Set up custom environment before nearly anything else is imported
# NOTE: this should be the first import (no not reorder)
from maskrcnn_benchmark.utils.env import setup_environment  # noqa F401 isort:skip

import argparse
import os

import torch
from maskrcnn_benchmark.config import cfg
from scene_graph_benchmark.config import sg_cfg
from maskrcnn_benchmark.data import make_data_loader
from maskrcnn_benchmark.data.datasets.utils.load_files import config_dataset_file
from maskrcnn_benchmark.solver import make_lr_scheduler
from maskrcnn_benchmark.solver import make_optimizer, make_optimizer_d2
from maskrcnn_benchmark.engine.inference import inference
from maskrcnn_benchmark.engine.trainer import do_train
from maskrcnn_benchmark.modeling.detector import build_detection_model
from scene_graph_benchmark.scene_parser import SceneParser
from scene_graph_benchmark.AttrRCNN import AttrRCNN
from maskrcnn_benchmark.utils.checkpoint import DetectronCheckpointer
from maskrcnn_benchmark.utils.collect_env import collect_env_info
from maskrcnn_benchmark.utils.comm import synchronize, get_rank
from maskrcnn_benchmark.utils.imports import import_file
from maskrcnn_benchmark.utils.logger import setup_logger
from maskrcnn_benchmark.utils.metric_logger import MetricLogger
from maskrcnn_benchmark.utils.miscellaneous import mkdir, save_config
from tools.test_sg_net import run_test

import random
import numpy as np

torch.manual_seed(1000)
torch.cuda.manual_seed(1000)
torch.cuda.manual_seed_all(1000)  # if you are using multi-GPU.
np.random.seed(1000)  # Numpy module.
random.seed(1000)  # Python random module.
torch.manual_seed(1000)
torch.backends.cudnn.benchmark = False
torch.backends.cudnn.deterministic = True


def train(cfg, local_rank, distributed):
    #建立模型
    if cfg.MODEL.META_ARCHITECTURE == "SceneParser":      
        model = SceneParser(cfg)
    elif cfg.MODEL.META_ARCHITECTURE == "AttrRCNN":
        model = AttrRCNN(cfg)
    device = torch.device(cfg.MODEL.DEVICE)
    model.to(device)

    if cfg.MODEL.BACKBONE.CONV_BODY.startswith("ViL"):    #CONV_BODY是主干网络的使用 oivrd：XNet-152-FPN   vgvrd: XNet-50-FPN   vgattr:前三 Vil 后一：XNet-152-C4  vrd:
        optimizer = make_optimizer_d2(cfg, model)
    else:
        optimizer = make_optimizer(cfg, model)
    scheduler = make_lr_scheduler(cfg, optimizer)         #其作用在于对optimizer中的学习率进行更新、调整，更新的方法是scheduler.step()。

    if distributed:
        model = torch.nn.parallel.DistributedDataParallel(    #进行分布式部署
            model, device_ids=[local_rank], output_device=local_rank,
            # this should be removed if we update BatchNorm stats
            broadcast_buffers=False, find_unused_parameters=True
        )
        
    # 创建一个参数字典，并将迭代次数置为0
    arguments = {}
    arguments["iteration"] = 0

    output_dir = cfg.OUTPUT_DIR

    save_to_disk = get_rank() == 0
    checkpointer = DetectronCheckpointer(
        cfg, model, optimizer, scheduler, output_dir, save_to_disk
    )
    extra_checkpoint_data = checkpointer.load(cfg.MODEL.WEIGHT)
    arguments.update(extra_checkpoint_data)

    data_loader = make_data_loader(
        cfg,
        is_train=True,
        is_distributed=distributed,
        start_iter=arguments["iteration"],
    )

    test_period = cfg.SOLVER.TEST_PERIOD
    if test_period > 0:
        data_loader_val = make_data_loader(cfg, is_train=False, is_distributed=distributed, is_for_period=True)
    else:
        data_loader_val = None

    checkpoint_period = cfg.SOLVER.CHECKPOINT_PERIOD
    
    meters = MetricLogger(delimiter="  ")

    do_train(
        cfg,
        model,
        data_loader,
        data_loader_val,
        optimizer,
        scheduler,
        checkpointer,
        device,
        checkpoint_period,
        test_period,
        arguments,
        meters
    )

    return model


def run_test(cfg, model, distributed):
    if distributed:
        model = model.module
    torch.cuda.empty_cache()  # TODO check if it helps
    iou_types = ("bbox",)
    if cfg.MODEL.MASK_ON:
        iou_types = iou_types + ("segm",)
    if cfg.MODEL.KEYPOINT_ON:
        iou_types = iou_types + ("keypoints",)
    output_folders = [None] * len(cfg.DATASETS.TEST)
    dataset_names = cfg.DATASETS.TEST
    if cfg.OUTPUT_DIR:
        for idx, dataset_name in enumerate(dataset_names):
            output_folder = os.path.join(cfg.OUTPUT_DIR, "inference", dataset_name)
            mkdir(output_folder)
            output_folders[idx] = output_folder
    data_loaders_val = make_data_loader(cfg, is_train=False, is_distributed=distributed)
    labelmap_file = config_dataset_file(cfg.DATA_DIR, cfg.DATASETS.LABELMAP_FILE)
    for output_folder, dataset_name, data_loader_val in zip(output_folders, dataset_names, data_loaders_val):
        inference(
            model,
            cfg,
            data_loader_val,
            dataset_name=dataset_name,
            iou_types=iou_types,
            box_only=False if cfg.MODEL.RETINANET_ON else cfg.MODEL.RPN_ONLY,
            bbox_aug=cfg.TEST.BBOX_AUG.ENABLED,
            device=cfg.MODEL.DEVICE,
            expected_results=cfg.TEST.EXPECTED_RESULTS,
            expected_results_sigma_tol=cfg.TEST.EXPECTED_RESULTS_SIGMA_TOL,
            output_folder=output_folder,
            skip_performance_eval=cfg.TEST.SKIP_PERFORMANCE_EVAL,
            labelmap_file=labelmap_file,
            save_predictions=cfg.TEST.SAVE_PREDICTIONS,
        )
        synchronize()


def main():
    parser = argparse.ArgumentParser(description="PyTorch Object Detection Training")
    parser.add_argument(
        "--config-file",                                    #配置文件
        default="",
        metavar="FILE",
        help="path to config file",
        type=str,
    )
    parser.add_argument("--local_rank", type=int, default=0)    #进程序号，就是用哪张卡
    parser.add_argument(
        "--skip-test",
        dest="skip_test",
        help="Do not test the final model",
        action="store_true",
    )
    parser.add_argument(
        "opts",                                                #使用命令行修改配置
        help="Modify config options using the command-line",
        default=None,
        nargs=argparse.REMAINDER,
    )

    args = parser.parse_args()

    num_gpus = int(os.environ["WORLD_SIZE"]) if "WORLD_SIZE" in os.environ else 1   #gpu数量
    args.distributed = num_gpus > 1

    if args.distributed:
        torch.cuda.set_device(args.local_rank)
        torch.distributed.init_process_group(
            backend="nccl", init_method="env://"
        )
        synchronize()
    
    #四个部分的配置融合 mask_rcnn原始配置 + scene_graph训练原始配置 + 训练不同模型的yaml配置 + 命令行修改的配置
    cfg.set_new_allowed(True)
    cfg.merge_from_other_cfg(sg_cfg)            #将scene_graph中的默认配置 加入到 原始mask_rcnn的默认配置中
    cfg.set_new_allowed(False)
    cfg.merge_from_file(args.config_file)       #将yaml文件加入到配置中
    cfg.merge_from_list(args.opts)              #将命令行输入的配置写入
    cfg.freeze()

    output_dir = cfg.OUTPUT_DIR    #在vggvrd 的redln的yaml文件中是 ./models/relation_danfeiX_FPN50/
    if output_dir:
        mkdir(output_dir)
    
    #日志文件的初始化
    logger = setup_logger("maskrcnn_benchmark", output_dir, get_rank())
    logger.info("Using {} GPUs".format(num_gpus))
    logger.info(args)

    logger.info("Collecting env info (might take some time)")
    logger.info("\n" + collect_env_info())

    logger.info("Loaded configuration file {}".format(args.config_file))
    with open(args.config_file, "r") as cf:
        config_str = "\n" + cf.read()
        logger.info(config_str)
    logger.info("Running with config:\n{}".format(cfg))

    output_config_path = os.path.join(cfg.OUTPUT_DIR, 'config.yml')   #输出的config文件在输出文件夹中
    logger.info("Saving config into: {}".format(output_config_path))
    # save overloaded model config in the output directory
    save_config(cfg, output_config_path)                              #将所有的配置（原始mask_rcnn配置 + scene_graph初始配置 + 训练模型的配置 +）添加到输出的config文件中

    model = train(cfg, args.local_rank, args.distributed)

    if not args.skip_test:
        run_test(cfg, model, args.distributed, model_name="model_final")


if __name__ == "__main__":
    main()
