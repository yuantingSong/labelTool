# --------------------------------------------------------
# SSH: Single Stage Headless Face Detector
# Test module for evaluating the SSH trained network
# Written by Mahyar Najibi
# --------------------------------------------------------
from __future__ import print_function
import cPickle
import os
import sys
import cv2
import numpy as np
import json
from PIL import Image
from utils.get_config import cfg, get_output_dir
from nms.nms_wrapper import nms
from utils.test_utils import _get_image_blob, _compute_scaling_factor, visusalize_detections
from utils.timer import Timer

def forward_net(net, blob, im_scale, pyramid='False'):
    """
    :param net: the trained network
    :param blob: a dictionary containing the image
    :param im_scale: the scale used for resizing the input image
    :param pyramid: whether using pyramid testing or not
    :return: the network outputs probs and pred_boxes (the probability of face/bg and the bounding boxes)
    """
    # Adding im_info to the data blob
    blob['im_info'] = np.array(
        [[blob['data'].shape[2], blob['data'].shape[3], im_scale]],
        dtype=np.float32)

    # Reshape network inputs
    net.blobs['data'].reshape(*(blob['data'].shape))
    net.blobs['im_info'].reshape(*(blob['im_info'].shape))

    # Forward the network
    net_args = {'data': blob['data'].astype(np.float32, copy=False),
                      'im_info': blob['im_info'].astype(np.float32, copy=False)}

    blobs_out = net.forward(**net_args)

    if pyramid:
        # If we are in the pyramid mode, return the outputs for different modules separately
        pred_boxes = []
        probs = []
        # Collect the outputs of the SSH detection modules
        for i in range(1,4):
            cur_boxes = net.blobs['m{}@ssh_boxes'.format(i)].data
            # unscale back to raw image space
            cur_boxes = cur_boxes[:, 1:5] / im_scale
            # Repeat boxes
            cur_probs = net.blobs['m{}@ssh_cls_prob'.format(i)].data
            pred_boxes.append(np.tile(cur_boxes, (1, cur_probs.shape[1])))
            probs.append(cur_probs)
    else:
        boxes = net.blobs['ssh_boxes'].data.copy()
        # unscale back to raw image space
        boxes = boxes[:, 1:5] / im_scale
        probs = blobs_out['ssh_cls_prob']
        pred_boxes = np.tile(boxes, (1, probs.shape[1]))

    return probs, pred_boxes


def detect(net, im_path, thresh=0.05, visualize=False, timers=None, pyramid=False, visualization_folder=None):
    """
    Main module to detect faces
    :param net: The trained network
    :param im_path: The path to the image
    :param thresh: Detection with a less score than thresh are ignored
    :param visualize: Whether to visualize the detections
    :param timers: Timers for calculating detect time (if None new timers would be created)
    :param pyramid: Whether to use pyramid during inference
    :param visualization_folder: If set the visualizations would be saved in this folder (if visualize=True)
    :return: cls_dets (bounding boxes concatenated with scores) and the timers
    """
    if not timers:
        timers = {'detect': Timer(),
                  'misc': Timer()}

    im = cv2.imread(im_path)
    imfname = os.path.basename(im_path)
    sys.stdout.flush()
    timers['detect'].tic()

    if not pyramid:
        im_scale = _compute_scaling_factor(im.shape,cfg.TEST.SCALES[0],cfg.TEST.MAX_SIZE)
        im_blob = _get_image_blob(im,[im_scale])
        probs, boxes = forward_net(net,im_blob[0],im_scale,False)
        boxes = boxes[:, 0:4]
    else:
        all_probs = []
        all_boxes = []
        # Compute the scaling coefficients for the pyramid
        # base_scale = _compute_scaling_factor(im.shape,cfg.TEST.PYRAMID_BASE_SIZE[0],cfg.TEST.PYRAMID_BASE_SIZE[1])
        base_scale = _compute_scaling_factor(im.shape,cfg.TEST.PYRAMID_BASE_SIZE[0],cfg.TEST.PYRAMID_BASE_SIZE[0])
        if im is None:
            return [],None
        # print(cfg.TEST.SCALES)
        pyramid_scales = [float(scale)/cfg.TEST.PYRAMID_BASE_SIZE[0]*base_scale
                          for scale in cfg.TEST.SCALES]

        im_blobs = _get_image_blob(im,pyramid_scales)

        for i in range(len(pyramid_scales)):
            probs,boxes = forward_net(net,im_blobs[i],pyramid_scales[i],True)
            for j in xrange(len(probs)):
                # Do not apply M3 to the largest scale
                if i<len(pyramid_scales)-1 or j<len(probs)-1:
                    all_boxes.append(boxes[j][:,0:4])
                    all_probs.append(probs[j].copy())

        probs = np.concatenate(all_probs)
        boxes = np.concatenate(all_boxes)

    timers['detect'].toc()
    timers['misc'].tic()

    inds = np.where(probs[:, 0] > thresh)[0]
    probs = probs[inds, 0]
    boxes = boxes[inds, :]
    dets = np.hstack((boxes, probs[:, np.newaxis])) \
            .astype(np.float32, copy=False)
    keep = nms(dets, cfg.TEST.NMS_THRESH)
    cls_dets = dets[keep, :]
    if visualize is None:
        plt_name = os.path.splitext(imfname)[0] + '_detections_{}'.format(net.name)
        visusalize_detections(im, cls_dets, plt_name=plt_name, visualization_folder=visualization_folder)
    timers['misc'].toc()
    return cls_dets,timers


def test_net(net, imdb, thresh=0.05, visualize=False,no_cache=False,output_path=None):
    """
    Testing the SSH network on a dataset
    :param net: The trained network
    :param imdb: The test imdb
    :param thresh: Detections with a probability less than this threshold are ignored
    :param visualize: Whether to visualize the detections
    :param no_cache: Whether to cache detections or not
    :param output_path: Output directory
    """
    # Initializing the timers
    print('Evaluating {} on {}'.format(net.name,imdb.name))
    timers = {'detect': Timer(), 'misc': Timer()}

    dets = [[[] for _ in xrange(len(imdb))] for _ in xrange(imdb.num_classes)]
    print('lmdb successful.')
    # NOTE: by default the detections for a given method is cached, set no_cache to disable caching!
    run_inference = True
    if not no_cache:
        output_dir = get_output_dir(imdb_name=imdb.name, net_name=net.name,output_dir=output_path)
        det_file = os.path.join(output_dir, 'detections.pkl')
        if os.path.exists(det_file) and not visualize:
            try:
                with open(det_file, 'r') as f:
                    dets = cPickle.load(f)
                    run_inference = False
                    print('Loading detections from cache: {}'.format(det_file))
            except:
                print('Could not load the cached detections file, detecting from scratch!')

    # Perform inference on images if necessary
    if run_inference:
        pyramid = True if len(cfg.TEST.SCALES)>1 else False

        for i in xrange(len(imdb)):
            im_path =imdb.image_path_at(i)
            dets[1][i], detect_time = detect(net, im_path, thresh, visualize=visualize,
                                             timers=timers, pyramid=pyramid)
            print('\r{:d}/{:d} detect-time: {:.3f}s, misc-time:{:.3f}s'
                  .format(i + 1, len(imdb), timers['detect'].average_time,
                          timers['misc'].average_time),end='')

        det_file = os.path.join(output_dir, 'detections.pkl')
        if not no_cache:
            with open(det_file, 'wb') as f:
                cPickle.dump(dets, f, cPickle.HIGHEST_PROTOCOL)
        print('\n', end='')

    # Evaluate the detections
    print('Evaluating detections')
    imdb.evaluate_detections(all_boxes=dets, output_dir=output_dir, method_name=net.name)
    print('All Done!')

def listDetect(net, listPath, savePath, saveTxt, thresh=0.05):
    timers = {'detect': Timer(), 'misc': Timer()}
    imlist = []
    for root, dirs, files in os.walk(listPath):
        for f in files:
            if f.endswith('jpg') or f.endswith('png'):
                imlist.append(os.path.join(root, f))
            print(os.path.join(root,f))
    count = 1

    fi_res = open(saveTxt, 'w')
    fi_res.close()
    fi_res=open(saveTxt,'a')
    for im_path in imlist:
        try:
            im = cv2.imread(im_path).shape
        except:
            continue
        ress=''
        dets, det_time = detect(net, im_path, thresh, True, timers, True, visualization_folder=savePath)
        print('\r{:d}/{:d} detect-time: {:.3f}s, misc-time:{:.3f}s'
              .format(count, len(imlist), timers['detect'].average_time,
                      timers['misc'].average_time))
       # print(dets)
        count += 1

       # detInfo += im_path + '\n'
        #detInfo += str(len(dets)) + '\n'
        #for det in dets:
           # detInfo += '\t'.join([str(d) for d in det]) + '\n'
        inds = np.where(dets[:, -1] >= 0.5)[0]
        bboxes = dets[inds]
        res = json.dumps({'id':im_path.split('/')[-2]+'/'+im_path.split('/')[-1], 'det_res':bboxes.tolist()},ensure_ascii=False) 
        ress += res + '\n'
        fi_res.write(ress + '\n')
        #im_name = im_path.split('/')[-1][:-4]
        #txt_frames_dir = os.path.split(im_path)[0].replace("frames", "frames_txt")
        #txt_frames_path = txt_frames_dir + "/" + im_name + ".txt"
        
       # print (txt_frames_path)
       # inds_liu = np.where(dets[:, -1] >=0.05)[0]
       # print ("len(dets): ", len(dets))
       # print ("inds_liu: ", inds_liu)

       # if (os.path.exists(txt_frames_dir)==False):
       #     os.mkdir(txt_frames_dir)
       # frames_res = open(txt_frames_path, 'w')
       # for det in dets:
       #     print (det[0], det[1], det[2], det[3], det[4])
       #     #frames_res.write(det[0] +  det[1] +  det[2] + det[3] + det[4] + '\n')
       #     frames_res.write('[' + str(det[0]) + '\t' + str(det[1]) + '\t' + str(det[2]) + '\t' + str(det[3]) + '\t' + str(det[4]) + ']' + '\n')

    fi_res.close()
   # f = open(saveTxt, 'w')
   # f.write(detInfo)
   # f.close()

def qiyiStarsDetect(net, listRootPath, saveRootPath, saveTxt, thresh = 0.05):
    txtTotalPath=saveTxt #'/root/data/yanglinxia/data/pol_check_haiyang_1_txt/'
    print('qiyis'+str(listRootPath))
    print()
    for root, dirs, files in os.walk(listRootPath):
        count = 0  
        for ds in dirs:
            if not os.listdir(os.path.join(root,ds)):
                continue
            count += 1
            total = len(dirs)
            listPath = os.path.join(listRootPath, ds)
            savePath = os.path.join(saveRootPath, ds)
            #if 125<= count<130 :
            if not os.path.exists(savePath):
                os.mkdir(savePath)
            saveTxt = os.path.join(txtTotalPath, ds+'.json')
            listDetect(net, listPath, savePath, saveTxt, thresh=0.05)
            print("current:%d/%d" %(count, total))
