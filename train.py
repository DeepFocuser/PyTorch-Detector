import glob
import logging
import os
import platform
import time
from collections import OrderedDict

import cv2
import mlflow as ml
import numpy as np
import torch
import torch.autograd as autograd
import torch.cuda.amp as amp
import torchvision
from torch.nn import DataParallel
from torch.optim import Adam, RMSprop, SGD, lr_scheduler
from torch.utils.tensorboard import SummaryWriter
from torchsummary import summary as modelsummary
from tqdm import tqdm

# from core import CenterNet
# from core import HeatmapFocalLoss, NormedL1Loss
# from core import Prediction
from core import Voc_2007_AP
from core import plot_bbox  # , export_block_for_cplusplus, PostNet
from core import traindataloader, validdataloader

logfilepath = ""
if os.path.isfile(logfilepath):
    os.remove(logfilepath)
logging.basicConfig(filename=logfilepath, level=logging.INFO)


def run(mean=[0.485, 0.456, 0.406],
        std=[0.229, 0.224, 0.225],
        epoch=100,
        input_size=[512, 512],
        input_frame_number=2,
        batch_size=16,
        batch_log=100,
        subdivision=4,
        train_dataset_path="Dataset/train",
        valid_dataset_path="Dataset/valid",
        data_augmentation=True,
        num_workers=4,
        optimizer="ADAM",
        lambda_off=1,
        lambda_size=0.1,
        save_period=5,
        load_period=10,
        learning_rate=0.001, decay_lr=0.999, decay_step=10,
        GPU_COUNT=0,
        base=18,
        pretrained_base=True,
        pretrained_path="modelparam",
        AMP=True,
        valid_size=8,
        eval_period=5,
        tensorboard=True,
        valid_graph_path="valid_Graph",
        valid_html_auto_open=True,
        using_mlflow=True,
        topk=100,
        iou_thresh=0.5,
        nms=False,
        except_class_thresh=0.01,
        nms_thresh=0.5,
        plot_class_thresh=0.5):

    if GPU_COUNT == 0:
        device = torch.device("cpu")
        AMP = False
    elif GPU_COUNT == 1:
        device = torch.device("cuda")
    else:
        device = [torch.device(f"cuda:{i}") for i in range(0, GPU_COUNT)]

    # 운영체제 확인
    if platform.system() == "Linux":
        logging.info(f"{platform.system()} OS")
    elif platform.system() == "Windows":
        logging.info(f"{platform.system()} OS")
    else:
        logging.info(f"{platform.system()} OS")

    # free memory는 정확하지 않은 것 같고, torch.cuda.max_memory_allocated() 가 정확히 어떻게 동작하는지?
    if isinstance(device, (list, tuple)):
        for i, d in enumerate(device):
            total_memory = torch.cuda.get_device_properties(d).total_memory
            free_memory = total_memory - torch.cuda.max_memory_allocated(d)
            free_memory = round(free_memory / (1024**3), 2)
            total_memory = round(total_memory / (1024**3), 2)
            logging.info(f'{torch.cuda.get_device_name(d)}')
            logging.info(f'Running on {d} / free memory : {free_memory}GB / total memory {total_memory}GB')
    else:
        if GPU_COUNT == 1:
            total_memory = torch.cuda.get_device_properties(device).total_memory
            free_memory = total_memory - torch.cuda.max_memory_allocated(device)
            free_memory = round(free_memory / (1024**3), 2)
            total_memory = round(total_memory / (1024**3), 2)
            logging.info(f'{torch.cuda.get_device_name(device)}')
            logging.info(f'Running on {device} / free memory : {free_memory}GB / total memory {total_memory}GB')
        else:
            logging.info(f'Running on {device}')

    if GPU_COUNT > 0 and batch_size < GPU_COUNT:
        logging.info("batch size must be greater than gpu number")
        exit(0)

    # if AMP:
    #     amp.init()

    if data_augmentation:
        logging.info("Using Data Augmentation")

    logging.info("training Center Detector")
    input_shape = (1, 3*input_frame_number) + tuple(input_size)

    scale_factor = 4  # 고정
    logging.info(f"scale factor {scale_factor}")

    train_dataloader, train_dataset = traindataloader(augmentation=data_augmentation,
                                                      path=train_dataset_path,
                                                      input_size=input_size,
                                                      input_frame_number=input_frame_number,
                                                      batch_size=batch_size,
                                                      pin_memory=True,
                                                      num_workers=num_workers,
                                                      shuffle=True, mean=mean, std=std, scale_factor=scale_factor,
                                                      make_target=True)

    train_update_number_per_epoch = len(train_dataloader)
    if train_update_number_per_epoch < 1:
        logging.warning("train batch size가 데이터 수보다 큼")
        exit(0)

    valid_list = glob.glob(os.path.join(valid_dataset_path, "*"))
    if valid_list:
        valid_dataloader, valid_dataset = validdataloader(path=valid_dataset_path,
                                                          input_size=input_size,
                                                          input_frame_number=input_frame_number,
                                                          batch_size=valid_size,
                                                          num_workers=num_workers,
                                                          pin_memory = True,
                                                          shuffle=True, mean=mean, std=std, scale_factor=scale_factor,
                                                          make_target=True)
        valid_update_number_per_epoch = len(valid_dataloader)
        if valid_update_number_per_epoch < 1:
            logging.warning("valid batch size가 데이터 수보다 큼")
            exit(0)

    num_classes = train_dataset.num_class  # 클래스 수
    name_classes = train_dataset.classes

    optimizer = optimizer.upper()
    if pretrained_base:
        model = str(input_size[0]) + "_" + str(input_size[1]) + "_" + optimizer + "_P" + "CENTER_RES" + str(base)
    else:
        model = str(input_size[0]) + "_" + str(input_size[1]) + "_" + optimizer + "_CENTER_RES" + str(base)

    # https://discuss.pytorch.org/t/how-to-save-the-optimizer-setting-in-a-log-in-pytorch/17187
    weight_path = os.path.join("weights", f"{model}")
    param_path = os.path.join(weight_path, f'{model}-{load_period:04d}.pt')

    start_epoch = 0
    net = CenterNet(base=base,
                    heads=OrderedDict([
                        ('heatmap', {'num_output': num_classes, 'bias': -2.19}),
                        ('offset', {'num_output': 2}),
                        ('wh', {'num_output': 2})
                    ]),
                    head_conv_channel=64,
                    pretrained=pretrained_base,
                    root=pretrained_path,
                    use_dcnv2=False)

    # https://github.com/sksq96/pytorch-summary
    if isinstance(device, (list, tuple)):
        modelsummary(net.to(device[0]), input_shape)
    else:
        modelsummary(net.to(device), input_shape)

    if tensorboard:
        summary = SummaryWriter(log_dir=os.path.join("torchboard", model), max_queue=10, flush_secs=10)
        if isinstance(device, (list, tuple)):
            summary.add_graph(net.to(device[0]), input_to_model=torch.ones(input_shape, device=device[0]), verbose=False)
        else:
            summary.add_graph(net.to(device), input_to_model=torch.ones(input_shape, device=device), verbose=False)

    if os.path.exists(param_path):
        start_epoch = load_period
        checkpoint = torch.load(param_path)
        if 'model_state_dict' in checkpoint:
            try:
                net.load_state_dict(checkpoint['model_state_dict'])
            except Exception as E:
                logging.info(E)
            else:
                logging.info(f"loading model_state_dict\n")

    if start_epoch + 1 >= epoch + 1:
        logging.info("this model has already been optimized")
        exit(0)

    if optimizer.upper() == "ADAM":
        trainer = Adam(net.parameters(), lr=learning_rate, betas=(0.9, 0.999), weight_decay=0.000001)
    elif optimizer.upper() == "RMSPROP":
        trainer = RMSprop(net.parameters(), lr=learning_rate, alpha=0.99, weight_decay=0.000001, momentum=0)
    elif optimizer.upper() == "SGD":
        trainer = SGD(net.parameters(), lr=learning_rate, momentum=0.9, weight_decay=0.000001)
    else:
        logging.error("optimizer not selected")
        exit(0)

    if os.path.exists(param_path):
        # optimizer weight 불러오기
        checkpoint = torch.load(param_path)
        if 'optimizer_state_dict' in checkpoint:
            try:
                trainer.load_state_dict(checkpoint['optimizer_state_dict'])
            except Exception as E:
                logging.info(E)
            else:
                logging.info(f"loading optimizer_state_dict\n")

    if GPU_COUNT > 0:
        # output_device=torch.device("cpu")? data parallel시 gpu 불균형 사용 막기 위함.
        net = DataParallel(net, device_ids=None, output_device=torch.device("cpu"), dim=0)

    # center net 병렬 처리, gradient, loss, prediction, preprocessing layer 업데이트 하기 등 알아보기
    # optimizer
    # https://pytorch.org/docs/master/optim.html?highlight=lr%20sche#torch.optim.lr_scheduler.CosineAnnealingLR
    unit = 1 if (len(train_dataset) // batch_size) < 1 else len(train_dataset) // batch_size
    step = unit * decay_step
    lr_sch = lr_scheduler.StepLR(trainer, step, gamma=decay_lr, last_epoch=-1)

    # if AMP:
    #     amp.init_trainer(trainer)

    heatmapfocalloss = HeatmapFocalLoss(from_sigmoid=True, alpha=2, beta=4)
    normedl1loss = NormedL1Loss()
    prediction = Prediction(batch_size=valid_size, topk=topk, scale=scale_factor, nms=nms, except_class_thresh=except_class_thresh, nms_thresh=nms_thresh)
    precision_recall = Voc_2007_AP(iou_thresh=iou_thresh, class_names=name_classes)

    start_time = time.time()
    for i in tqdm(range(start_epoch + 1, epoch + 1, 1), initial=start_epoch + 1, total=epoch):

        heatmap_loss_sum = 0
        offset_loss_sum = 0
        wh_loss_sum = 0
        time_stamp = time.time()

        # multiscale을 하게되면 여기서 train_dataloader을 다시 만드는 것이 좋겠군..
        for batch_count, (image, _, heatmap_target, offset_target, wh_target, mask_target, _) in enumerate(
                train_dataloader,
                start=1):

            td_batch_size = image.shape[0]
            trainer.zero_grad()

            if GPU_COUNT <= 1:
                image = image.to(device)
                heatmap_target = heatmap_target.to(device)
                offset_target = offset_target.to(device)
                wh_target = wh_target.to(device)
                mask_target= mask_target.to(device)

            image_split = torch.split(image, subdivision, dim=0)
            heatmap_target_split = torch.split(heatmap_target, subdivision, dim=0)
            offset_target_split = torch.split(offset_target, subdivision, dim=0)
            wh_target_split = torch.split(wh_target, subdivision, dim=0)
            mask_target_split = torch.split(mask_target, subdivision, dim=0)

            heatmap_losses = []
            offset_losses = []
            wh_losses = []
            total_loss = []

            for image_part, heatmap_target_part, offset_target_part, wh_target_part, mask_target_part in zip(image_split,
                                                                                                             heatmap_target_split,
                                                                                                             offset_target_split,
                                                                                                             wh_target_split,
                                                                                                             mask_target_split):

                heatmap_pred, offset_pred, wh_pred = net(image_part)
                heatmap_loss = heatmapfocalloss(heatmap_pred, heatmap_target_part)
                offset_loss = normedl1loss(offset_pred, offset_target_part, mask_target_part) * lambda_off
                wh_loss = normedl1loss(wh_pred, wh_target_part, mask_target_part) * lambda_size

                heatmap_losses.append(heatmap_loss.item())
                offset_losses.append(offset_loss.item())
                wh_losses.append(wh_loss.item())
                total_loss.append(heatmap_loss + offset_loss + wh_loss)

            if AMP:
                with amp.scale_loss(total_loss, trainer) as scaled_loss:
                    autograd.backward(scaled_loss)
            else:
                autograd.backward(total_loss)

            trainer.step()
            lr_sch.step()

            heatmap_loss_sum += sum(heatmap_losses) / td_batch_size
            offset_loss_sum += sum(offset_losses) / td_batch_size
            wh_loss_sum += sum(wh_losses) / td_batch_size

            if batch_count % batch_log == 0:
                logging.info(f'[Epoch {i}][Batch {batch_count}/{train_update_number_per_epoch}],'
                             f'[Speed {td_batch_size / (time.time() - time_stamp):.3f} samples/sec],'
                             f'[Lr = {trainer.learning_rate}]'
                             f'[heatmap loss = {sum(heatmap_losses) / td_batch_size:.3f}]'
                             f'[offset loss = {sum(offset_losses) / td_batch_size:.3f}]'
                             f'[wh loss = {sum(wh_losses) / td_batch_size:.3f}]')
            time_stamp = time.time()

        train_heatmap_loss_mean = np.divide(heatmap_loss_sum, train_update_number_per_epoch)
        train_offset_loss_mean = np.divide(offset_loss_sum, train_update_number_per_epoch)
        train_wh_loss_mean = np.divide(wh_loss_sum, train_update_number_per_epoch)
        train_total_loss_mean = train_heatmap_loss_mean + train_offset_loss_mean + train_wh_loss_mean

        logging.info(
            f"train heatmap loss : {train_heatmap_loss_mean} / train offset loss : {train_offset_loss_mean} / train wh loss : {train_wh_loss_mean} / train total loss : {train_total_loss_mean}")

        if i % save_period == 0:

            weight_epoch_path = os.path.join(weight_path, str(i))
            if not os.path.exists(weight_epoch_path):
                os.makedirs(weight_epoch_path)

            # optimizer weight 저장하기
            try:
                trainer.save_states(os.path.join(weight_path, f'{model}-{i:04d}.opt'))
            except Exception as E:
                logging.error(f"optimizer weight export 예외 발생 : {E}")
            else:
                logging.info("optimizer weight export 성공")

            '''
            Hybrid models can be serialized as JSON files using the export function
            Export HybridBlock to json format that can be loaded by SymbolBlock.imports, mxnet.mod.Module or the C++ interface.
            When there are only one input, it will have name data. When there Are more than one inputs, they will be named as data0, data1, etc.
            '''
            if GPU_COUNT >= 1:
                context = mx.gpu(0)
            else:
                context = mx.cpu(0)

            '''
                mxnet1.6.0 버전 에서 AMP 사용시 위에 미리 선언한 prediction을 사용하면 문제가 될 수 있다. 
                -yolo v3, gaussian yolo v3 에서는 문제가 발생한다.
                mxnet 1.5.x 버전에서는 아래와 같이 새로 선언하지 않아도 정상 동작한다.  

                block들은 함수 인자로 보낼 경우 자기 자신이 보내진다.(복사되는 것이 아님)
                export_block_for_cplusplus 에서 prediction 이 hybridize 되면서 
                미리 선언한 prediction도 hybridize화 되면서 symbol 형태가 된다. 
                이런 현상을 보면 아래와같이 다시 선언해 주는게 맞는 것 같다.
            '''
            auxnet = Prediction(topk=topk, scale=scale_factor, nms=nms, except_class_thresh=except_class_thresh, nms_thresh=nms_thresh)
            postnet = PostNet(net=net, auxnet=auxnet)  # 새로운 객체가 생성
            try:
                net.export(os.path.join(weight_path, f"{model}"), epoch=i, remove_amp_cast=True)
                net.save_parameters(os.path.join(weight_path, f"{i}.params"))  # onnx 추출용
                # network inference, decoder, nms까지 처리됨 - mxnet c++에서 편리함
                export_block_for_cplusplus(input_frame_number=input_frame_number, path=os.path.join(weight_epoch_path, f"{model}_prepost"),
                                           block=postnet,
                                           data_shape=tuple(input_size) + tuple((3*input_frame_number,)),
                                           epoch=i,
                                           preprocess=True,  # c++ 에서 inference시 opencv에서 읽은 이미지 그대로 넣으면 됨
                                           layout='HWC',
                                           ctx=context,
                                           remove_amp_cast=True)
            except Exception as E:
                logging.error(f"jit, pt export 예외 발생 : {E}")
            else:
                logging.info("jit, pt export 성공")
                net.collect_params().reset_ctx(ctx)

        if i % eval_period == 0 and valid_list:

            heatmap_loss_sum = 0
            offset_loss_sum = 0
            wh_loss_sum = 0

            # loss 구하기
            for image, label, heatmap_target, offset_target, wh_target, mask_target, _ in valid_dataloader:

                vd_batch_size = image.shape[0]

                if GPU_COUNT <= 1:
                    image = image.to(device)
                    label = label.to(device)
                    heatmap_target = heatmap_target.to(device)
                    offset_target = offset_target.to(device)
                    wh_target = wh_target.to(device)
                    mask_target = mask_target.to(device)

                gt_box = label[:, :, :4]
                gt_id = label[:, :, 4:5]
                heatmap_pred, offset_pred, wh_pred = net(image)
                id, score, bbox = prediction(heatmap_pred, offset_pred, wh_pred)
                precision_recall.update(pred_bboxes=bbox,
                                        pred_labels=id,
                                        pred_scores=score,
                                        gt_boxes=gt_box * scale_factor,
                                        gt_labels=gt_id)

                heatmap_loss = heatmapfocalloss(heatmap_pred, heatmap_target)
                offset_loss = normedl1loss(offset_pred, offset_target, mask_target) * lambda_off
                wh_loss = normedl1loss(wh_pred, wh_target, mask_target) * lambda_size

                heatmap_loss_sum += heatmap_loss.item() / vd_batch_size
                offset_loss_sum += offset_loss.item() / vd_batch_size
                wh_loss_sum += wh_loss.item() / vd_batch_size

            valid_heatmap_loss_mean = np.divide(heatmap_loss_sum, valid_update_number_per_epoch)
            valid_offset_loss_mean = np.divide(offset_loss_sum, valid_update_number_per_epoch)
            valid_wh_loss_mean = np.divide(wh_loss_sum, valid_update_number_per_epoch)
            valid_total_loss_mean = valid_heatmap_loss_mean + valid_offset_loss_mean + valid_wh_loss_mean

            logging.info(
                f"valid heatmap loss : {valid_heatmap_loss_mean} / valid offset loss : {valid_offset_loss_mean} / valid wh loss : {valid_wh_loss_mean} / valid total loss : {valid_total_loss_mean}")

            AP_appender = []
            round_position = 2
            class_name, precision, recall, true_positive, false_positive, threshold = precision_recall.get_PR_list()
            for j, c, p, r in zip(range(len(recall)), class_name, precision, recall):
                name, AP = precision_recall.get_AP(c, p, r)
                logging.info(f"class {j}'s {name} AP : {round(AP * 100, round_position)}%")
                AP_appender.append(AP)
            mAP_result = np.mean(AP_appender)

            logging.info(f"mAP : {round(mAP_result * 100, round_position)}%")
            precision_recall.get_PR_curve(name=class_name,
                                          precision=precision,
                                          recall=recall,
                                          threshold=threshold,
                                          AP=AP_appender, mAP=mAP_result, folder_name=valid_graph_path, epoch=i,
                                          auto_open=valid_html_auto_open)
            precision_recall.reset()

            if tensorboard:
                # gpu N 개를 대비한 코드 (Data Parallelism)
                dataloader_iter = iter(valid_dataloader)
                image, label, _, _, _, _, _ = next(dataloader_iter)

                if GPU_COUNT <= 1:
                    image = image.to(device)
                    label = label.to(device)

                ground_truth_colors = {}
                for k in range(num_classes):
                    ground_truth_colors[k] = (0, 0, 1)

                batch_image = []
                gt_boxes = label[:, :, :4]
                gt_ids = label[:, :, 4:5]
                heatmap_pred, offset_pred, wh_pred = net(image)
                ids, scores, bboxes = prediction(heatmap_pred, offset_pred, wh_pred)

                # numpy로 바꾸기
                image = image.cpu().numpy()
                gt_ids = gt_ids.cpu().numpy()
                gt_boxes = gt_boxes.cpu().numpy()
                heatmap_pred = heatmap_pred.cpu().numpy()
                ids = ids.cpu().numpy()
                scores = scores.cpu().numpy()
                bboxes = bboxes.cpu().numpy()

                for img, gt_id, gt_box, heatmap, id, score, bbox in zip(image, gt_ids, gt_boxes, heatmap_pred, ids,
                                                                        scores, bboxes):
                    split_img = np.split(img, input_frame_number, axis=0)
                    hconcat_image_list = []
                    for j, ig in enumerate(split_img):

                        ig = ig.transpose((1, 2, 0)) * np.array(std) + np.array(mean)
                        ig = (ig * 255).clip(0, 255)

                        if j == len(split_img)-1: # 마지막 이미지
                            # heatmap 그리기
                            heatmap = mx.nd.multiply(heatmap, 255.0)  # 0 ~ 255 범위로 바꾸기
                            heatmap = mx.nd.max(heatmap, axis=0, keepdims=True)  # channel 축으로 가장 큰것 뽑기
                            heatmap = mx.nd.transpose(heatmap, axes=(1, 2, 0))  # (height, width, channel=1)
                            heatmap = mx.nd.repeat(heatmap, repeats=3, axis=-1)  # (height, width, channel=3)
                            heatmap = heatmap.asnumpy()  # mxnet.ndarray -> numpy.ndarray
                            heatmap = heatmap.astype("uint8")  # float32 -> uint8
                            heatmap = cv2.resize(heatmap, dsize=(input_size[1], input_size[0]))  # 사이즈 원복
                            heatmap = cv2.applyColorMap(heatmap, cv2.COLORMAP_JET)

                            # ground truth box 그리기
                            ground_truth = plot_bbox(ig, gt_box * scale_factor, scores=None, labels=gt_id, thresh=None,
                                                     reverse_rgb=True,
                                                     class_names=valid_dataset.classes, absolute_coordinates=True,
                                                     colors=ground_truth_colors)
                            # prediction box 그리기
                            prediction_box = plot_bbox(ground_truth, bbox, scores=score, labels=id,
                                                       thresh=plot_class_thresh,
                                                       reverse_rgb=False,
                                                       class_names=valid_dataset.classes, absolute_coordinates=True)
                            hconcat_image_list.append(prediction_box)
                            hconcat_image_list.append(heatmap)
                        else:
                            ig = ig.astype(np.uint8)
                            hconcat_image_list.append(ig)

                    hconcat_images = np.concatenate(hconcat_image_list, axis=1)

                    # Tensorboard에 그리기 위해 BGR -> RGB / (height, width, channel) -> (channel, height, width) 를한다.
                    hconcat_images = cv2.cvtColor(hconcat_images, cv2.COLOR_BGR2RGB)
                    hconcat_images = np.transpose(hconcat_images, axes=(2, 0, 1))
                    batch_image.append(hconcat_images)  # (batch, channel, height, width)

                img_grid = torchvision.utils.make_grid(batch_image, nrow=1)
                summary.add_image(tag="valid_result", img_tensor = img_grid, global_step=i)
                summary.add_scalar(tag="heatmap_loss", scalar_value={"train_heatmap_loss_mean": train_heatmap_loss_mean,
                                                                     "valid_heatmap_loss_mean": valid_heatmap_loss_mean},
                                   global_step=i)
                summary.add_scalar(tag="offset_loss",
                                   scalar_value={"train_offset_loss_mean": train_offset_loss_mean,
                                                 "valid_offset_loss_mean": valid_offset_loss_mean},
                                   global_step=i)
                summary.add_scalar(tag="wh_loss",
                                   scalar_value={"train_wh_loss_mean": train_wh_loss_mean,
                                                 "valid_wh_loss_mean": valid_wh_loss_mean},
                                   global_step=i)

                summary.add_scalar(tag="total_loss", scalar_value={
                    "train_total_loss": train_total_loss_mean,
                    "valid_total_loss": valid_total_loss_mean},
                                   global_step=i)

                for name, param in net.named_parameters():
                    summary.add_histogram(tag=name, values=param, global_step=i)

    end_time = time.time()
    learning_time = end_time - start_time
    logging.info(f"learning time : 약, {learning_time / 3600:0.2f}H")
    logging.info("optimization completed")

    if using_mlflow:
        ml.log_metric("learning time", round(learning_time / 3600, 2))


if __name__ == "__main__":
    run(mean=[0.485, 0.456, 0.406],
        std=[0.229, 0.224, 0.225],
        epoch=100,
        input_size=[512, 512],
        input_frame_number=2,
        batch_size=16,
        batch_log=100,
        subdivision=4,
        train_dataset_path="Dataset/train",
        valid_dataset_path="Dataset/valid",
        data_augmentation=True,
        num_workers=4,
        optimizer="ADAM",
        lambda_off=1,
        lambda_size=0.1,
        save_period=5,
        load_period=10,
        learning_rate=0.001, decay_lr=0.999, decay_step=10,
        GPU_COUNT=0,
        base=18,
        pretrained_base=True,
        pretrained_path="modelparam",
        AMP=True,
        valid_size=8,
        eval_period=5,
        tensorboard=True,
        valid_graph_path="valid_Graph",
        valid_html_auto_open=True,
        using_mlflow=True,
        topk=100,
        iou_thresh=0.5,
        nms=False,
        except_class_thresh=0.01,
        nms_thresh=0.5,
        plot_class_thresh=0.5)
