"""Test a model and generate submission CSV.

Usage:
    > python test.py --split SPLIT --load_path PATH --name NAME
    where
    > SPLIT is either "dev" or "test"
    > PATH is a path to a checkpoint (e.g., save/train/model-01/best.pth.tar)
    > NAME is a name to identify the test run

Author:
    Chris Chute (chute@stanford.edu)
"""

import csv
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.utils.data as data
import util

from args import get_test_args
from collections import OrderedDict
from json import dumps
from models import BiDAF, BiDAF_character, QANet
from os.path import join
from tensorboardX import SummaryWriter
from tqdm import tqdm
from ujson import load as json_load
from util import collate_fn, SQuAD


def main(args):
    # Set up logging
    args.save_dir = util.get_save_dir(args.save_dir, args.name, training=False)
    log = util.get_logger(args.save_dir, args.name)
    log.info(f'Args: {dumps(vars(args), indent=4, sort_keys=True)}')
    device, gpu_ids = util.get_available_devices()
    args.batch_size *= max(1, len(gpu_ids))

    # Get embeddings
    log.info('Loading embeddings...')
    word_vectors = util.torch_from_json(args.word_emb_file)

    # Get model
    log.info('Building model...')
    if(args.model_type == "baseline"):
        model = BiDAF(word_vectors=word_vectors,
                    hidden_size=args.hidden_size,
                    drop_prob=0)
    elif(args.model_type == "bidaf_char"):
        char_vectors = util.torch_from_json(args.char_emb_file)
        model = BiDAF_character(word_vectors=word_vectors,
                    char_vectors=char_vectors,
                    hidden_size=args.hidden_size,
                    drop_prob=0)
    elif(args.model_type == "QANet"):
        char_vectors = util.torch_from_json(args.char_emb_file)

        model = QANet(word_vectors=word_vectors,
                            char_vectors=char_vectors,
                            hidden_size=args.hidden_size,
                            drop_prob=0, 
                            device = device)
        

        
    else:
        raise Exception("Model provided not valid")
    if(len(args.ensemble_list) != 0):


        # Get data loader
        log.info('Building dataset...')
        record_file = vars(args)[f'{args.split}_record_file']
        dataset = SQuAD(record_file, args.use_squad_v2)
        data_loader = data.DataLoader(dataset,
                                    batch_size=args.batch_size,
                                    shuffle=False,
                                    num_workers=args.num_workers,
                                    collate_fn=collate_fn)

        # Evaluate
        log.info(f'Evaluating on {args.split} split...')
        nll_meter = util.AverageMeter()
        pred_dict = {}  # Predictions for TensorBoard
        sub_dict = {}   # Predictions for submission
        eval_file = vars(args)[f'{args.split}_eval_file']
        with open(eval_file, 'r') as fh:
            gold_dict = json_load(fh)
        with torch.no_grad(), \
                tqdm(total=len(dataset)) as progress_bar:
            for cw_idxs, cc_idxs, qw_idxs, qc_idxs, y1, y2, ids in data_loader:
                cw_idxs = cw_idxs.to(device)
                qw_idxs = qw_idxs.to(device)
                batch_size = args.batch_size

                avg_log_p1 = torch.zeros(cw_idxs.size())
                avg_log_p2 = torch.zeros(cw_idxs.size())
                # to device
                avg_log_p1, avg_log_p2 = avg_log_p1.to(device), avg_log_p2.to(device)

                for load_path in args.ensemble_list:
                    if(args.model_type == "baseline"):
                        model = BiDAF(word_vectors=word_vectors,
                                    hidden_size=args.hidden_size,
                                    drop_prob=0)
                    elif(args.model_type == "bidaf_char"):
                        char_vectors = util.torch_from_json(args.char_emb_file)
                        model = BiDAF_character(word_vectors=word_vectors,
                                    char_vectors=char_vectors,
                                    hidden_size=args.hidden_size,
                                    drop_prob=0)
                    elif(args.model_type == "QANet"):
                        char_vectors = util.torch_from_json(args.char_emb_file)

                        model = QANet(word_vectors=word_vectors,
                                            char_vectors=char_vectors,
                                            hidden_size=args.hidden_size,
                                            drop_prob=0, 
                                            device = device) 
                    else:          
                        raise Exception("Model provided not valid")         
                    model = nn.DataParallel(model, gpu_ids)
                    log.info(f'Loading checkpoint from {load_path}...')
                    model = util.load_model(model, load_path, gpu_ids, return_step=False)
                    model = model.to(device)
                    model.eval()
                    if(args.model_type == "baseline"):
                        log_p1, log_p2 = model(cw_idxs, qw_idxs)
                    elif(args.model_type == "bidaf_char"):
                        log_p1, log_p2 = model(cw_idxs, qw_idxs, cc_idxs, qc_idxs)
                    elif(args.model_type == "QANet"):
                        log_p1, log_p2 = model(cw_idxs, qw_idxs, cc_idxs, qc_idxs)
                    else:
                        raise Exception("Model Type Invalid")
                    avg_log_p1 = avg_log_p1 + log_p1
                    avg_log_p2 = avg_log_p2 + log_p2
                # Setup for forward


                # Forward
                avg_log_p1 /= len(args.ensemble_list)
                avg_log_p2 /= len(args.ensemble_list)

                p1, p2 = avg_log_p1.exp(), avg_log_p2.exp()
                y1, y2 = y1.to(device), y2.to(device)
                loss = F.nll_loss(avg_log_p1, y1) + F.nll_loss(avg_log_p2, y2)
                nll_meter.update(loss.item(), batch_size)


                # Get F1 and EM scores

                starts, ends = util.discretize(p1, p2, args.max_ans_len, args.use_squad_v2)

                # Log info
                progress_bar.update(batch_size)
                if args.split != 'test':
                    # No labels for the test set, so NLL would be invalid
                    progress_bar.set_postfix(NLL=nll_meter.avg)

                idx2pred, uuid2pred = util.convert_tokens(gold_dict,
                                                        ids.tolist(),
                                                        starts.tolist(),
                                                        ends.tolist(),
                                                        args.use_squad_v2)
                pred_dict.update(idx2pred)
                sub_dict.update(uuid2pred)

        # Log results (except for test set, since it does not come with labels)
        if args.split != 'test':
            results = util.eval_dicts(gold_dict, pred_dict, args.use_squad_v2)
            results_list = [('NLL', nll_meter.avg),
                            ('F1', results['F1']),
                            ('EM', results['EM'])]
            if args.use_squad_v2:
                results_list.append(('AvNA', results['AvNA']))
            results = OrderedDict(results_list)

            # Log to console
            results_str = ', '.join(f'{k}: {v:05.2f}' for k, v in results.items())
            log.info(f'{args.split.title()} {results_str}')

            # Log to TensorBoard
            tbx = SummaryWriter(args.save_dir)
            util.visualize(tbx,
                        pred_dict=pred_dict,
                        eval_path=eval_file,
                        step=0,
                        split=args.split,
                        num_visuals=args.num_visuals)

        # Write submission file
        sub_path = join(args.save_dir, args.split + '_' + args.sub_file)
        log.info(f'Writing submission file to {sub_path}...')
        with open(sub_path, 'w', newline='', encoding='utf-8') as csv_fh:
            csv_writer = csv.writer(csv_fh, delimiter=',')
            csv_writer.writerow(['Id', 'Predicted'])
            for uuid in sorted(sub_dict):
                csv_writer.writerow([uuid, sub_dict[uuid]])
                
    else: # ensemble version 
        model = nn.DataParallel(model, gpu_ids)
        log.info(f'Loading checkpoint from {args.load_path}...')
        

        # Get data loader
        log.info('Building dataset...')
        record_file = vars(args)[f'{args.split}_record_file']
        dataset = SQuAD(record_file, args.use_squad_v2)
        data_loader = data.DataLoader(dataset,
                                    batch_size=args.batch_size,
                                    shuffle=False,
                                    num_workers=args.num_workers,
                                    collate_fn=collate_fn)

        # Evaluate
        log.info(f'Evaluating on {args.split} split...')
        nll_meter = util.AverageMeter()
        pred_dict = {}  # Predictions for TensorBoard
        sub_dict = {}   # Predictions for submission
        eval_file = vars(args)[f'{args.split}_eval_file']
        with open(eval_file, 'r') as fh:
            gold_dict = json_load(fh)

        prob_results = []
        for model_path in args.ensemble_list:
            model = util.load_model(model, model_path, gpu_ids, return_step=False)
            model = model.to(device)
            model.eval()

            with torch.no_grad(), \
                    tqdm(total=len(dataset)) as progress_bar:
                for cw_idxs, cc_idxs, qw_idxs, qc_idxs, y1, y2, ids in data_loader:
                    # Setup for forward
                    cw_idxs = cw_idxs.to(device)
                    qw_idxs = qw_idxs.to(device)
                    batch_size = args.batch_size

                    # Forward
                    if(args.model_type == "baseline"):
                        log_p1, log_p2 = model(cw_idxs, qw_idxs)
                    elif(args.model_type == "bidaf_char"):
                        log_p1, log_p2 = model(cw_idxs, qw_idxs, cc_idxs, qc_idxs)
                    elif(args.model_type == "QANet"):
                        log_p1, log_p2 = model(cw_idxs, qw_idxs, cc_idxs, qc_idxs)
                    else:
                        raise Exception("Model Type Invalid")
                    y1, y2 = y1.to(device), y2.to(device)
                    loss = F.nll_loss(log_p1, y1) + F.nll_loss(log_p2, y2)
                    nll_meter.update(loss.item(), batch_size)

                    # Get F1 and EM scores
                    p1, p2 = log_p1.exp(), log_p2.exp()
                    prob_results.append(tuple(p1,p2))
        
                starts, ends = util.discretize(p1, p2, args.max_ans_len, args.use_squad_v2)

                # Log info
                progress_bar.update(batch_size)
                if args.split != 'test':
                    # No labels for the test set, so NLL would be invalid
                    progress_bar.set_postfix(NLL=nll_meter.avg)

                idx2pred, uuid2pred = util.convert_tokens(gold_dict,
                                                        ids.tolist(),
                                                        starts.tolist(),
                                                        ends.tolist(),
                                                        args.use_squad_v2)
                pred_dict.update(idx2pred)
                sub_dict.update(uuid2pred)

        # Log results (except for test set, since it does not come with labels)
        if args.split != 'test':
            results = util.eval_dicts(gold_dict, pred_dict, args.use_squad_v2)
            results_list = [('NLL', nll_meter.avg),
                            ('F1', results['F1']),
                            ('EM', results['EM'])]
            if args.use_squad_v2:
                results_list.append(('AvNA', results['AvNA']))
            results = OrderedDict(results_list)

            # Log to console
            results_str = ', '.join(f'{k}: {v:05.2f}' for k, v in results.items())
            log.info(f'{args.split.title()} {results_str}')

            # Log to TensorBoard
            tbx = SummaryWriter(args.save_dir)
            util.visualize(tbx,
                        pred_dict=pred_dict,
                        eval_path=eval_file,
                        step=0,
                        split=args.split,
                        num_visuals=args.num_visuals)

        # Write submission file
        sub_path = join(args.save_dir, args.split + '_' + args.sub_file)
        log.info(f'Writing submission file to {sub_path}...')
        with open(sub_path, 'w', newline='', encoding='utf-8') as csv_fh:
            csv_writer = csv.writer(csv_fh, delimiter=',')
            csv_writer.writerow(['Id', 'Predicted'])
            for uuid in sorted(sub_dict):
                csv_writer.writerow([uuid, sub_dict[uuid]])



if __name__ == '__main__':
    main(get_test_args())
