from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence


class ForestModelError(RuntimeError):
    """A validated forest model cannot be evaluated safely."""


@dataclass(frozen=True, slots=True)
class ProbabilityTree:
    children_left: tuple[int, ...]
    children_right: tuple[int, ...]
    feature: tuple[int, ...]
    threshold: tuple[float, ...]
    positive_probability: tuple[float, ...]

    def predict_probability(self, row: Sequence[float]) -> float:
        node = 0
        seen = 0
        while True:
            if node < 0 or node >= len(self.feature):
                raise ForestModelError("tree node index out of range")
            left = self.children_left[node]
            right = self.children_right[node]
            if left == -1 and right == -1:
                p = self.positive_probability[node]
                return max(0.0, min(1.0, float(p)))
            feature_index = self.feature[node]
            if feature_index < 0 or feature_index >= len(row):
                raise ForestModelError("tree feature index out of range")
            node = left if row[feature_index] <= self.threshold[node] else right
            seen += 1
            if seen > len(self.feature):
                raise ForestModelError("tree traversal cycle detected")


@dataclass(frozen=True, slots=True)
class ForestClassifierSpec:
    model_id: str
    version: str
    direction: str
    horizon_minutes: int
    feature_names: tuple[str, ...]
    trees: tuple[ProbabilityTree, ...]
    favourable_move_bps: float
    adverse_move_bps: float
    validation_id: str

    def validate(self) -> None:
        if not self.model_id.strip() or not self.version.strip() or not self.validation_id.strip():
            raise ForestModelError("model_id/version/validation_id are required")
        if self.direction not in {"LONG", "SHORT"}:
            raise ForestModelError("direction must be LONG or SHORT")
        if self.horizon_minutes <= 0:
            raise ForestModelError("horizon_minutes must be positive")
        if not self.feature_names or len(set(self.feature_names)) != len(self.feature_names):
            raise ForestModelError("feature_names must be unique and non-empty")
        if not self.trees:
            raise ForestModelError("at least one tree is required")
        if self.favourable_move_bps <= 0 or self.adverse_move_bps <= 0:
            raise ForestModelError("favourable/adverse move assumptions must be positive")

    def probability(self, features: Mapping[str, float]) -> float:
        self.validate()
        missing = [name for name in self.feature_names if name not in features]
        if missing:
            raise ForestModelError(f"missing meta features: {missing}")
        row: list[float] = []
        for name in self.feature_names:
            value = float(features[name])
            if value != value or value in (float("inf"), float("-inf")):
                raise ForestModelError(f"invalid meta feature: {name}")
            row.append(value)
        return sum(tree.predict_probability(row) for tree in self.trees) / len(self.trees)

    def expected_gross_return_bps(self, features: Mapping[str, float]) -> float:
        p = self.probability(features)
        return p * self.favourable_move_bps - (1.0 - p) * self.adverse_move_bps

    def to_dict(self) -> dict:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "direction": self.direction,
            "horizon_minutes": self.horizon_minutes,
            "feature_names": list(self.feature_names),
            "trees": [
                {
                    "children_left": list(tree.children_left),
                    "children_right": list(tree.children_right),
                    "feature": list(tree.feature),
                    "threshold": list(tree.threshold),
                    "positive_probability": list(tree.positive_probability),
                }
                for tree in self.trees
            ],
            "favourable_move_bps": self.favourable_move_bps,
            "adverse_move_bps": self.adverse_move_bps,
            "validation_id": self.validation_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "ForestClassifierSpec":
        trees = tuple(
            ProbabilityTree(
                children_left=tuple(int(v) for v in item["children_left"]),
                children_right=tuple(int(v) for v in item["children_right"]),
                feature=tuple(int(v) for v in item["feature"]),
                threshold=tuple(float(v) for v in item["threshold"]),
                positive_probability=tuple(float(v) for v in item["positive_probability"]),
            )
            for item in payload["trees"]
        )
        spec = cls(
            model_id=str(payload["model_id"]),
            version=str(payload["version"]),
            direction=str(payload["direction"]),
            horizon_minutes=int(payload["horizon_minutes"]),
            feature_names=tuple(str(v) for v in payload["feature_names"]),
            trees=trees,
            favourable_move_bps=float(payload["favourable_move_bps"]),
            adverse_move_bps=float(payload["adverse_move_bps"]),
            validation_id=str(payload["validation_id"]),
        )
        spec.validate()
        return spec


def export_random_forest_classifier(
    *,
    model,
    model_id: str,
    version: str,
    direction: str,
    horizon_minutes: int,
    feature_names: Sequence[str],
    favourable_move_bps: float,
    adverse_move_bps: float,
    validation_id: str,
) -> ForestClassifierSpec:
    """Export sklearn RandomForestClassifier into a standard-library runtime spec."""
    classes = list(model.classes_)
    if 1 not in classes:
        raise ForestModelError("classifier does not contain positive class 1")
    positive_index = classes.index(1)
    trees: list[ProbabilityTree] = []
    for estimator in model.estimators_:
        tree = estimator.tree_
        probabilities: list[float] = []
        for raw in tree.value:
            values = raw[0]
            total = float(sum(values))
            probabilities.append(
                float(values[positive_index]) / total if total > 0 else 0.0
            )
        trees.append(
            ProbabilityTree(
                children_left=tuple(int(v) for v in tree.children_left),
                children_right=tuple(int(v) for v in tree.children_right),
                feature=tuple(int(v) for v in tree.feature),
                threshold=tuple(float(v) for v in tree.threshold),
                positive_probability=tuple(probabilities),
            )
        )
    spec = ForestClassifierSpec(
        model_id=model_id,
        version=version,
        direction=direction,
        horizon_minutes=horizon_minutes,
        feature_names=tuple(feature_names),
        trees=tuple(trees),
        favourable_move_bps=float(favourable_move_bps),
        adverse_move_bps=float(adverse_move_bps),
        validation_id=validation_id,
    )
    spec.validate()
    return spec
