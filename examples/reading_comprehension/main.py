import glob
import json
import logging
import os
import random
from argparse import Namespace
from functools import partial

import click
import torch
from torch.utils.data import DataLoader, RandomSampler
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm
from transformers import WEIGHTS_NAME, BertTokenizer

from ..model import two_stage_model_args
from ..trainer import Trainer, trainer_args
from ..utils import set_seed
from .model import LukeForReadingComprehension
from .utils.dataset import RECORD_HIGHLIGHT_TOKEN, RECORD_PLACEHOLDER_TOKEN, RecordProcessor, SquadV1Processor,\
    SquadV2Processor
from .utils.feature_generator import convert_examples_to_features
from .utils.record_eval import evaluate as evaluate_on_record
from .utils.result_writer import Result, write_predictions
from .utils.squad_eval import EVAL_OPTS as SQUAD_EVAL_OPTS
from .utils.squad_eval import main as evaluate_on_squad

logger = logging.getLogger(__name__)


@click.group(name='reading-comprehension')
def cli():
    pass


@cli.command()
@click.option('--data-dir', default='data/squad', type=click.Path(exists=True))
@click.option('--dataset-type', default='squad', type=click.Choice(['squad', 'record']))
@click.option('--with-negative/--no-negative', default=True)
@click.option('--null-score-diff-threshold', type=float, default=0.0)
@click.option('--doc-stride', default=128)
@click.option('--max-query-length', default=64)
@click.option('--n-best-size', default=20)
@click.option('--max-answer-length', default=30)
@click.option('--max-seq-length', default=512)
@click.option('--max-entity-length', default=128)
@click.option('--max-candidate-length', default=30)
@click.option('--do-train/--no-train', default=True)
@click.option('--do-eval/--no-eval', default=True)
@click.option('--create-cache', is_flag=True)
@click.option('--overwrite-cache', is_flag=True)
@click.option('--train-batch-size', default=1)
@click.option('--eval-batch-size', default=8)
@click.option('--num-train-epochs', default=2)
@click.option('--fix-entity-emb/--update-entity-emb', default=True)
@click.option('--no-entity', is_flag=True, default=False)
@click.option('--use-first-answer/--use-random-answer', default=True)
@click.option('--eval-all-checkpoints/--no-eval-checkpoints', default=False)
@click.option('--seed', default=42)
@two_stage_model_args
@trainer_args
@click.pass_obj
def run(common_args, **task_args):
    task_args.update(common_args)
    args = Namespace(**task_args)

    set_seed(args.seed)

    if 'roberta' in args.bert_model_name:
        args.tokenizer.tokenize = partial(args.tokenizer.tokenize, add_prefix_space=True)

    if args.dataset_type == 'record':
        args.tokenizer.add_special_tokens(
            dict(additional_special_tokens=[RECORD_HIGHLIGHT_TOKEN, RECORD_PLACEHOLDER_TOKEN]))

    if args.create_cache:
        load_and_cache_examples(args, evaluate=False)
        load_and_cache_examples(args, evaluate=True)
        return

    if args.dataset_type == 'record':
        args.model_config.vocab_size += 2
        new_emb = torch.empty(2, args.model_config.hidden_size)
        new_emb.normal_(mean=0.0, std=args.model_config.initializer_range)
        args.model_weights['embeddings.word_embeddings.weight'] = torch.cat(
            [args.model_weights['embeddings.word_embeddings.weight'], new_emb])

    if args.do_train:
        model = LukeForReadingComprehension(args)
        model.load_state_dict(args.model_weights, strict=False)
        model.to(args.device)

        if args.fix_entity_emb:
            model.entity_embeddings.entity_embeddings.weight.requires_grad = False

        train_dataloader, _, _, _ = load_and_cache_examples(args, evaluate=False)

        num_train_steps = len(train_dataloader) // args.gradient_accumulation_steps * args.num_train_epochs
        trainer = Trainer(args, model=model, dataloader=train_dataloader, num_train_steps=num_train_steps)
        trainer.train()

    if args.do_train and args.local_rank in (0, -1):
        if hasattr(model, 'module'):
            torch.save(model.module.state_dict(), os.path.join(args.output_dir, WEIGHTS_NAME))
        else:
            torch.save(model.state_dict(), os.path.join(args.output_dir, WEIGHTS_NAME))

    model = None
    torch.cuda.empty_cache()
    if args.local_rank != -1:
        torch.distributed.barrier()

    results = {}
    if args.do_eval and args.local_rank in (-1, 0):
        checkpoints = [args.output_dir]
        if args.eval_all_checkpoints:
            checkpoints = list(os.path.dirname(c) for c in sorted(glob.glob(args.output_dir + '/**/' + WEIGHTS_NAME,
                                                                            recursive=True)))

        for checkpoint_dir in checkpoints:
            global_step = checkpoint_dir.split('-')[-1] if len(checkpoints) > 1 else ""
            model = LukeForReadingComprehension(args)
            model.load_state_dict(torch.load(os.path.join(checkpoint_dir, WEIGHTS_NAME), map_location='cpu'))
            model.to(args.device)

            if torch.cuda.device_count() > 1:
                model = torch.nn.DataParallel(model)

            result = evaluate(args, model, prefix=global_step)
            result = {k + '_' + str(global_step) if global_step else k: v for k, v in result.items()}
            results.update(result)

    logger.info('Results: %s', results)
    print(results)
    return results


def evaluate(args, model, prefix=''):
    dataloader, examples, features, processor = load_and_cache_examples(args, evaluate=True)
    all_results = []
    for batch in tqdm(dataloader, desc="Eval"):
        model.eval()
        inputs = {k: v.to(args.device) for k, v in batch.items() if k != 'example_indices'}
        with torch.no_grad():
            outputs = model(**inputs)

        for i, example_index in enumerate(batch['example_indices']):
            eval_feature = features[example_index.item()]
            unique_id = int(eval_feature.unique_id)
            start_logits, end_logits = [o[i].detach().cpu().tolist() for o in outputs]
            all_results.append(Result(unique_id, start_logits, end_logits))

    output_prediction_file = os.path.join(args.output_dir, 'predictions_{}.json'.format(prefix))
    output_nbest_file = os.path.join(args.output_dir, 'nbest_predictions_{}.json'.format(prefix))
    if args.with_negative:
        output_null_log_odds_file = os.path.join(args.output_dir, 'null_odds_{}.json'.format(prefix))
    else:
        output_null_log_odds_file = None

    do_lower_case = False
    if isinstance(args.tokenizer, BertTokenizer):
        do_lower_case = args.tokenizer.basic_tokenizer.do_lower_case

    write_predictions(examples, features, all_results, args.n_best_size, args.max_answer_length, do_lower_case,
                      output_prediction_file, output_nbest_file, output_null_log_odds_file, False,
                      args.with_negative, args.null_score_diff_threshold, args.tokenizer)

    if args.dataset_type == 'record':
        with open(os.path.join(args.data_dir, processor.dev_file)) as f:
            dev_data = json.load(f)['data']
        with open(output_prediction_file) as f:
            predictions = json.load(f)
        return evaluate_on_record(dev_data, predictions)[0]
    else:
        return evaluate_on_squad(SQUAD_EVAL_OPTS(os.path.join(args.data_dir, processor.dev_file),
                                                 pred_file=output_prediction_file,
                                                 na_prob_file=output_null_log_odds_file))


def load_and_cache_examples(args, evaluate=False):
    if args.local_rank not in (-1, 0) and not evaluate:
        torch.distributed.barrier()

    if args.dataset_type == 'record':
        processor = RecordProcessor()
    elif args.with_negative:
        processor = SquadV2Processor()
    else:
        processor = SquadV1Processor()

    if evaluate:
        examples = processor.get_dev_examples(args.data_dir)
    else:
        examples = processor.get_train_examples(args.data_dir)

    bert_model_name = args.model_config.bert_model_name

    cache_file = os.path.join(args.data_dir, 'cached_' + '_'.join((
        bert_model_name.split('-')[0],
        str(len(args.entity_vocab)),
        os.path.basename(args.mention_db.mention_db_file),
        str(args.max_seq_length),
        str(args.max_mention_length),
        str(args.max_candidate_length),
        str(args.doc_stride),
        str(args.max_query_length),
        str(evaluate),
        str(args.with_negative)
    )) + '.pkl')
    if os.path.exists(cache_file) and not args.overwrite_cache:
        logger.info('Loading features from the cached file %s', cache_file)
        features = torch.load(cache_file)
    else:
        logger.info('Creating features from the dataset...')

        segment_b_id = 1
        add_extra_sep_token = False
        if 'roberta' in bert_model_name:
            segment_b_id = 0
            add_extra_sep_token = True

        features = convert_examples_to_features(
            examples, args.tokenizer, args.entity_vocab, args.mention_db, args.max_seq_length, args.max_mention_length,
            args.max_candidate_length, args.doc_stride, args.max_query_length, segment_b_id, add_extra_sep_token,
            not evaluate)

        if args.local_rank in (-1, 0):
            torch.save(features, cache_file)

    if args.local_rank == 0 and not evaluate:
        torch.distributed.barrier()

    def collate_fn(batch):
        def create_padded_sequence(attr_name, padding_value):
            tensors = [torch.tensor(getattr(o[1], attr_name), dtype=torch.long) for o in batch]
            return torch.nn.utils.rnn.pad_sequence(tensors, batch_first=True, padding_value=padding_value)

        ret = dict(
            word_ids=create_padded_sequence('word_ids', args.tokenizer.pad_token_id),
            word_attention_mask=create_padded_sequence('word_attention_mask', 0),
            word_segment_ids=create_padded_sequence('word_segment_ids', 0),
            entity_candidate_ids=create_padded_sequence('entity_candidate_ids', 0)[:, :args.max_entity_length, :],
            entity_attention_mask=create_padded_sequence('entity_attention_mask', 0)[:, :args.max_entity_length],
            entity_position_ids=create_padded_sequence('entity_position_ids', -1)[:, :args.max_entity_length, :],
            entity_segment_ids=create_padded_sequence('entity_segment_ids', 0)[:, :args.max_entity_length],
        )
        if args.no_entity:
            ret['entity_attention_mask'].fill_(0)

        if evaluate:
            ret['example_indices'] = torch.tensor([o[0] for o in batch], dtype=torch.long)
        else:
            if args.dataset_type == 'record':
                ret['start_positions'] = torch.tensor([random.choice(o[1].start_positions) for o in batch],
                                                      dtype=torch.long)
                ret['end_positions'] = torch.tensor([random.choice(o[1].end_positions) for o in batch],
                                                    dtype=torch.long)
            else:
                ret['start_positions'] = torch.tensor([o[1].start_positions[0] for o in batch], dtype=torch.long)
                ret['end_positions'] = torch.tensor([o[1].end_positions[0] for o in batch], dtype=torch.long)

        return ret

    if evaluate:
        dataloader = DataLoader(list(enumerate(features)), batch_size=args.eval_batch_size, collate_fn=collate_fn)
    else:
        if args.local_rank == -1:
            sampler = RandomSampler(features)
        else:
            sampler = DistributedSampler(features)
        dataloader = DataLoader(list(enumerate(features)), sampler=sampler, batch_size=args.train_batch_size,
                                collate_fn=collate_fn)

    return dataloader, examples, features, processor