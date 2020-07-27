import torch
import torch.nn as nn

class Prediction(nn.Module):

    def __init__(self, batch_size=1, topk=100, scale=4.0, nms=False, except_class_thresh=0.01, nms_thresh=0.5):
        super(Prediction, self).__init__()
        self._batch_size = batch_size
        self._topk = topk
        self._scale = scale
        self._heatmap_nms = nn.MaxPool2d(3, stride=1, padding=1)
        self._nms = nms
        self._nms_thresh = nms_thresh
        self._except_class_thresh = except_class_thresh

    def _non_maximum_suppression(self, ids, scores, bboxes):

        # https://gist.github.com/mkocabas/a2f565b27331af0da740c11c78699185
        '''
        1. 내림차순 정렬 하기
        2. non maximum suppression 후 빈 박스들은  -1로 채우기

        :param ids: (object number, 1)
        :param scores: (object number, 1)
        :param bboxes: (object number, 4)
        :return: ids, scores, bboxes
        '''

        if ids.shape[0] == 1:
            return ids, scores, bboxes

        # 내림차순 정렬
        indices = scores.argsort(dim=0, descending=True)[:,0] # 내림차순 정렬
        ids = ids[indices]
        scores = scores[indices]
        xmin = bboxes[:,0:1][indices]
        ymin = bboxes[:,1:2][indices]
        xmax = bboxes[:,2:3][indices]
        ymax = bboxes[:,3:4][indices]

        # nms 알고리즘
        x1 = xmin[:, 0]
        y1 = ymin[:, 0]
        x2 = xmax[:, 0]
        y2 = ymax[:, 0]
        mask = torch.ones_like(x1)
        i = 0
        while i < len(ids)-1:
            xx1 = torch.max(x1[i], x1[i+1:])
            yy1 = torch.max(y1[i], y1[i+1:])
            xx2 = torch.max(x2[i], x2[i+1:])
            yy2 = torch.max(y2[i], y2[i+1:])
            w = xx2 - xx1 + 1
            h = yy2 - yy1 + 1
            overlap = (w * h) / (x1[i]+x1[i+1:]-(w * h))
            mask[i+1:] = torch.where(overlap > self._nms_thresh, torch.ones_like(overlap), torch.ones_like(overlap)*-1)
            i+=1

        # nms 한 것들 mask 씌우기
        mask = mask[:,None]
        ids = ids*mask
        scores = scores * mask
        xmin = xmin * mask
        ymin = ymin * mask
        xmax = xmax * mask
        ymax = ymax * mask

        # nms 한 것들 mask 씌우기
        ids = torch.where(ids<0, ids*-1, ids)
        scores = torch.where(scores<0, scores*-1, scores)
        xmin = torch.where(xmin<0, xmin*-1, xmin)
        ymin = torch.where(ymin<0, ymin*-1, ymin)
        xmax = torch.where(xmax<0, xmax*-1, xmax)
        ymax = torch.where(ymax<0, ymax*-1, ymax)
        bboxes = torch.cat([xmin, ymin, xmax, ymax], dim=-1)

        return ids, scores, bboxes

    def forward(self, heatmap, offset, wh):
        '''
        The peak keypoint extraction serves
        as a sufficient NMS alternative and can be implemented efficiently on device using a 3 × 3 max pooling operation.
        '''
        keep = self._heatmap_nms(heatmap) == heatmap
        heatmap = torch.mul(keep, heatmap)
        batch, channel, height, width = heatmap.shape

        # 상위 self._topk개만 뽑아내기
        heatmap_resize = heatmap.reshape((batch, -1))
        scores, indices = heatmap_resize.topk(k=self._topk, dim=-1, largest=True, sorted=True)  #(batch, channel * height * width) / int64

        scores = scores[:,:,None]
        ids = torch.floor_divide(indices, (height * width)) # 몫 만 구하기
        ids = ids.float()  # c++에서 float으로 받아오기 때문에!!! 형 변환 필요
        ids = ids[:,:,None]

        '''
        박스 복구
        To limit the computational burden, we use a single size prediction  WxHx2
        for all object categories. 
        offset, wh에 해당 
        '''
        offset = offset.permute(0, 2, 3, 1).reshape(
            (batch, -1, 2))  # (batch, x, y, channel) -> (batch, height*width, 2)
        wh = wh.permute(0, 2, 3, 1).reshape((batch, -1, 2))  # (batch, width, height, channel) -> (batch, height*width, 2)
        topk_indices = torch.fmod(indices, (height * width))  # 클래스별 index

        # 2차원 복구
        topk_ys = torch.floor_divide(topk_indices, width)  # y축 index
        topk_xs = torch.fmod(topk_indices, width)  # x축 index

        batch_indices = torch.arange(self._batch_size, device=ids.device)
        batch_indices = batch_indices[:offset.shape[0], None]
        batch_indices = batch_indices.repeat_interleave(self._topk, dim=-1) # (batch, self._topk)

        offset_xs_indices = torch.zeros_like(batch_indices, dtype=torch.int64)
        offset_ys_indices = torch.ones_like(batch_indices, dtype=torch.int64)

        offset_xs = torch.cat((batch_indices, topk_indices, offset_xs_indices), dim=0).reshape((3, -1))
        offset_ys = torch.cat((batch_indices, topk_indices, offset_ys_indices), dim=0).reshape((3, -1))

        # 핵심
        # (batch, height*width, 2) / (3(각 인덱스), self_batch_size*self._topk)
        xs = offset[offset_xs[0], offset_xs[1], offset_xs[2]].reshape((-1, self._topk))
        ys = offset[offset_ys[0], offset_ys[1], offset_ys[2]].reshape((-1, self._topk))
        topk_xs = topk_xs.float() + xs
        topk_ys = topk_ys.float() + ys

        # (batch, height*width, 2) / (3(각 인덱스), self_batch_size*self._topk)
        w = wh[offset_xs[0], offset_xs[1], offset_xs[2]].reshape((-1, self._topk))
        h = wh[offset_ys[0], offset_ys[1], offset_ys[2]].reshape((-1, self._topk))

        half_w = w / 2
        half_h = h / 2
        bboxes = [topk_xs - half_w, topk_ys - half_h, topk_xs + half_w, topk_ys + half_h]  # 각각 (batch, self._topk)
        bboxes = torch.cat([bbox[:,:,None] for bbox in bboxes], dim=-1)  # (batch, self._topk, 1) ->  (batch, self._topk, 4)

        except_mask = scores > self._except_class_thresh
        ids = torch.where(except_mask, ids, torch.ones_like(ids) * -1)
        scores = torch.where(except_mask, scores, torch.ones_like(scores) * -1)
        xmin = torch.where(except_mask, bboxes[:,:,0:1], torch.ones_like(bboxes[:,:,0:1]) * -1)
        ymin = torch.where(except_mask, bboxes[:,:,1:2], torch.ones_like(bboxes[:,:,1:2]) * -1)
        xmax = torch.where(except_mask, bboxes[:,:,2:3], torch.ones_like(bboxes[:,:,2:3]) * -1)
        ymax = torch.where(except_mask, bboxes[:,:,3:4], torch.ones_like(bboxes[:,:,3:4]) * -1)

        if self._nms:
            if self._nms_thresh > 0 and self._nms_thresh < 1:

                ids_list = []
                scores_list = []
                bboxes_list = []

                # id별로 나누기
                unique_id = ids.unique().to(torch.long)

                # batch 별로 나누기
                for id, score, x_min, y_min, x_max, y_max in zip(ids, scores, xmin, ymin, xmax, ymax):

                    id_list = []
                    score_list = []
                    bbox_list = []

                    for uid in unique_id:
                        bbox = torch.cat([x_min[id==uid, None],y_min[id==uid, None], x_max[id==uid, None], y_max[id==uid, None]], dim=-1)

                        id_part, score_part, bbox_part = self._non_maximum_suppression(id[id==uid, None], score[id==uid, None], bbox)

                        id_list.append(id_part)
                        score_list.append(score_part)
                        bbox_list.append(bbox_part)
                    id_concat = torch.cat(id_list, dim=0)
                    score_concat = torch.cat(score_list, dim=0)
                    bbox_concat = torch.cat(bbox_list, dim=0)

                    ids_list.append(id_concat)
                    scores_list.append(score_concat)
                    bboxes_list.append(bbox_concat)

                # batch 차원
                ids_concat = torch.stack(ids_list, dim=0)
                scores_concat = torch.stack(scores_list, dim=0)
                bboxes_concat = torch.stack(bboxes_list, dim=0)

            return ids_concat, scores_concat, bboxes_concat * self._scale
        else:
            bboxes = torch.cat([xmin, ymin, xmax, ymax], dim=-1)
            return ids, scores, bboxes * self._scale

# test
if __name__ == "__main__":
    import os
    from collections import OrderedDict
    from core import CenterNet

    input_size = (512, 512)
    scale_factor = 4
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

    '''
    heatmap의 bias가 -2.19 인 이유는??? retinanet의 식과 같은데... 흠..
    For the final conv layer of the classification subnet, we set the bias initialization to b = − log((1 − π)/π),
    where π specifies that at the start of training every anchor should be labeled as foreground with confidence of ∼π.
    We use π = .01 in all experiments, although results are robust to the exact value. As explained in §3.3, 
    this initialization prevents the large number of background anchors from generating a large, 
    destabilizing loss value in the first iteration of training
    '''
    net = CenterNet(base=18,
                    input_frame_number=2,
                    heads=OrderedDict([
                        ('heatmap', {'num_output': 5, 'bias': -2.19}),
                        ('offset', {'num_output': 2}),
                        ('wh', {'num_output': 2})
                    ]),
                    head_conv_channel=64,
                    pretrained=False)


    prediction = Prediction(batch_size=10, topk=7, scale=scale_factor, nms=True, except_class_thresh=0.01, nms_thresh=0.5)
    heatmap, offset, wh = net(torch.rand(5, 6, input_size[0], input_size[1]))
    ids, scores, bboxes = prediction(heatmap, offset, wh)

    print(f"< input size(height, width) : {input_size} >")
    print(f"topk class id shape : {ids.shape}")
    print(f"topk class scores shape : {scores.shape}")
    print(f"topk box predictions shape : {bboxes.shape}")
    '''
    < input size(height, width) : (512, 512) >
    topk class id shape : torch.Size([1, 100, 1])
    topk class scores shape : torch.Size([1, 100, 1])
    topk box predictions shape : torch.Size([1, 100, 4])
    '''
