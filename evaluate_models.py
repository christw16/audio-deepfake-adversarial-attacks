import argparse
import logging
from pathlib import Path
from typing import Dict, List, Optional, Union
from unittest.mock import MagicMock

import torch
import tqdm
import yaml
import neptune.new as neptune
from sklearn.metrics import precision_recall_fscore_support, roc_auc_score
from torch.utils.data import DataLoader

from dfadetect import cnn_features, metrics, utils, neptune_utils
from dfadetect.agnostic_datasets.attack_agnostic_dataset import AttackAgnosticDataset, NoFoldDataset
from dfadetect.models import models
from dfadetect.trainer import NNDataSetting

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

ch = logging.StreamHandler()
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
ch.setFormatter(formatter)
LOGGER.addHandler(ch)


def get_dataset(
    datasets_paths: List[Union[Path, str]],
    fold: int,
    amount_to_use: Optional[int],
) -> Union[AttackAgnosticDataset, NoFoldDataset]:
    if fold == -1:
        data_val = NoFoldDataset(
            asvspoof_path=datasets_paths[0],
            wavefake_path=datasets_paths[1],
            fakeavceleb_path=datasets_paths[2],
            fold_num=fold,
            fold_subset="val",
            reduced_number=10_000,
        )
    else:
        data_val = AttackAgnosticDataset(
            asvspoof_path=datasets_paths[0],
            wavefake_path=datasets_paths[1],
            fakeavceleb_path=datasets_paths[2],
            fold_num=fold,
            fold_subset="val",
            reduced_number=amount_to_use,
        )
    return data_val


def evaluate_nn(
    model_paths: List[Path],
    datasets_paths: List[Union[Path, str]],
    data_config: Dict,
    model_config: Dict,
    device: str,
    metrics_logger: Union[neptune.metadata_containers.run.Run, MagicMock],
    amount_to_use: Optional[int] = None,
    batch_size: int = 128,
    no_fold: bool = False,
):
    # TODO: add neptune support & EER support
    LOGGER.info("Loading data...")
    model_name, model_parameters = model_config["name"], model_config["parameters"]
    use_cnn_features = False if model_name in ["rawnet", "rawnet3", "frontend_lcnn", "frontend_specrnet"] else True
    cnn_features_setting = data_config.get("cnn_features_setting", None)

    nn_data_setting = NNDataSetting(
        use_cnn_features=use_cnn_features,
    )

    if use_cnn_features:
        cnn_features_setting = cnn_features.CNNFeaturesSetting(**cnn_features_setting)
    else:
        cnn_features_setting = cnn_features.CNNFeaturesSetting()

    weights_path = ""
    folds = [0, 1, 2] if not no_fold else [-1]

    for fold_no, fold in enumerate(tqdm.tqdm(folds)):
        # Load model architecture
        model = models.get_model(
            model_name=model_name,
            config=model_parameters,
            device=device,
        )
        # If provided weights, apply corresponding ones (from an appropriate fold)
        if len(model_paths) >= 1:
            assert len(model_paths) == 3 or len(model_paths) == 1, "Pass either 0, 1 or 3 weights path"
            weights_path = model_paths[fold]
            model.load_state_dict(torch.load(weights_path))
        model = model.to(device)

        logging_prefix = f"fold_{fold}"

        data_val = get_dataset(
            datasets_paths=datasets_paths,
            fold=fold,
            amount_to_use=amount_to_use,
        )

        LOGGER.info(
            f"Testing '{model_name}' model, weights path: '{weights_path}', on {len(data_val)} audio files."
        )
        print(f"Test Fold [{fold_no+1}/{len(folds)}]: ")
        test_loader = DataLoader(
            data_val,
            batch_size=batch_size,
            shuffle=True,
            drop_last=True,
            num_workers=3,
        )

        batches_number = len(data_val) // batch_size
        num_correct = 0.0
        num_total = 0.0
        y_pred = torch.Tensor([]).to(device)
        y = torch.Tensor([]).to(device)
        y_pred_label = torch.Tensor([]).to(device)

        for i, (batch_x, _, batch_y) in enumerate(test_loader):
            model.eval()
            if i % 10 == 0:
                print(f"Batch [{i}/{batches_number}]")

            with torch.no_grad():
                batch_x = batch_x.to(device)
                batch_y = batch_y.to(device)
                num_total += batch_x.size(0)

                if nn_data_setting.use_cnn_features:
                    batch_x = cnn_features.prepare_feature_vector(
                        batch_x, cnn_features_setting=cnn_features_setting
                    )

                batch_pred = model(batch_x).squeeze(1)
                batch_pred = torch.sigmoid(batch_pred)
                batch_pred_label = (batch_pred + .5).int()

                num_correct += (batch_pred_label == batch_y.int()).sum(dim=0).item()

                y_pred = torch.concat([y_pred, batch_pred], dim=0)
                y_pred_label = torch.concat([y_pred_label, batch_pred_label], dim=0)
                y = torch.concat([y, batch_y], dim=0)

        eval_accuracy = (num_correct / num_total) * 100

        precision, recall, f1_score, support = precision_recall_fscore_support(
            y.cpu().numpy(), y_pred_label.cpu().numpy(), average="binary", beta=1.0
        )
        auc_score = roc_auc_score(y_true=y.cpu().numpy(), y_score=y_pred.cpu().numpy())

        # For EER flip values, following original evaluation implementation
        y_for_eer = 1 - y

        thresh, eer, fpr, tpr = metrics.calculate_eer(
            y=y_for_eer.cpu().numpy(),
            y_score=y_pred.cpu().numpy(),
        )

        eer_label = f"eval/{logging_prefix}__eer"
        accuracy_label = f"eval/{logging_prefix}__accuracy"
        precision_label = f"eval/{logging_prefix}__precision"
        recall_label = f"eval/{logging_prefix}__recall"
        f1_label = f"eval/{logging_prefix}__f1_score"
        auc_label = f"eval/{logging_prefix}__auc"

        # Log metrics ...
        metrics_logger[eer_label].log(eer)
        metrics_logger[accuracy_label].log(eval_accuracy)
        metrics_logger[precision_label].log(precision)
        metrics_logger[recall_label].log(recall)
        metrics_logger[f1_label].log(f1_score)
        metrics_logger[auc_label].log(auc_score)

        LOGGER.info(
            f"{eer_label}: {eer:.4f}, {accuracy_label}: {eval_accuracy:.4f}, {precision_label}: {precision:.4f}, {recall_label}: {recall:.4f}, {f1_label}: {f1_score:.4f}, {auc_label}: {auc_score:.4f}"
        )


def main(args):

    if not args.cpu and torch.cuda.is_available():
        device = "cuda"
    else:
        device = "cpu"

    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    seed = config["data"].get("seed", 42)
    # fix all seeds - this should not actually change anything
    utils.set_seed(seed)

    try:
        from neptune.new.integrations.python_logger import NeptuneHandler

        log_metrics = config["logging"]["log_metrics"]
        experiment_id = config["logging"]["existing_experiment_id"]
        # Workaround for not overwriting existing neptune tags, description and name
        config["logging"] = {
            "existing_experiment_id": experiment_id,
        }
        neptune_instance = neptune_utils.get_metric_logger(
            should_log_metrics=log_metrics, config=config
        )
        npt_handler = NeptuneHandler(run=neptune_instance)
        LOGGER.addHandler(npt_handler)
    except:
        LOGGER.info("Neptune not initialized - using mocked logger!")
        neptune_instance = MagicMock()

    neptune_instance["sys/tags"].add(config["model"]["name"])
    neptune_instance["sys/tags"].add("eval")
    neptune_instance["sys/tags"].add("eval")
    neptune_instance["sys/tags"].add(f"seed_{config['data'].get('seed', '?')}")
    neptune_instance["source_code/config"].upload(args.config)

    evaluate_nn(
        model_paths=config["checkpoint"].get("paths", []),
        datasets_paths=[args.asv_path, args.wavefake_path, args.celeb_path],
        model_config=config["model"],
        data_config=config["data"],
        amount_to_use=args.amount,
        device=device,
        metrics_logger=neptune_instance,

        no_fold=args.no_fold,
    )

def parse_args():
    parser = argparse.ArgumentParser()

    # If assigned as None, then it won't be taken into account
    ASVSPOOF_DATASET_PATH = "/home/adminuser/storage/datasets/deep_fakes/ASVspoof2021/LA"
    WAVEFAKE_DATASET_PATH = "/home/adminuser/storage/datasets/deep_fakes/WaveFake"
    FAKEAVCELEB_DATASET_PATH = "/home/adminuser/storage/datasets/deep_fakes/FakeAVCeleb/FakeAVCeleb_v1.2"

    parser.add_argument("--asv_path", type=str, default=ASVSPOOF_DATASET_PATH)
    parser.add_argument("--wavefake_path", type=str, default=WAVEFAKE_DATASET_PATH)
    parser.add_argument("--celeb_path", type=str, default=FAKEAVCELEB_DATASET_PATH)

    default_model_config = "config.yaml"
    parser.add_argument(
        "--config",
        help="Model config file path (default: config.yaml)",
        type=str,
        default=default_model_config,
    )

    default_amount = None
    parser.add_argument(
        "--amount",
        "-a",
        help=f"Amount of files to load from each directory (default: {default_amount} - use all).",
        type=int,
        default=default_amount,
    )

    parser.add_argument("--cpu", "-c", help="Force using cpu", action="store_true")

    parser.add_argument(
        "--no_fold", help="Use no fold version of the dataset", action="store_true"
    )

    return parser.parse_args()


if __name__ == "__main__":
    main(parse_args())