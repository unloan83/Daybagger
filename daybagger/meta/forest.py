from __future__ import annotations

from dataclasses import dataclass
from math import sqrt
from typing import Mapping, Sequence


class ForestModelError(RuntimeError):
    """A validated forest model cannot be evaluated safely."""


@dataclass(frozen=True, slots=True)
class RegressionTree:
    children_left: tuple[int, ...]
    children_right: tuple[int, ...]
    feature: tuple[int, ...]
    threshold: tuple[float, ...]
    value_bps: tuple[float, ...]

    def predict(self, row: Sequence[float]) -> float:
        node = 0
        seen = 0
        while True:
            if node < 0 or node >= len(self.feature):
                raise ForestModelError("tree node index out of range")
            left = self.children_left[node]
            right = self.children_right[node]
            if left == -1 and right == -1:
                return float(self.value_bps[node])
            feature_index = self.feature[node]
            if feature_index < 0 or feature_index >= len(row):
                raise ForestModelError("tree feature index out of range")
            node = left if row[feature_index] <= self.threshold[node] else right
            seen += 1
            if seen > len(self.feature):
                raise ForestModelError("tree traversal cycle detected")


@dataclass(frozen=True, slots=True)
class ForestRegressorSpec:
    """
    Standard-library runtime representation of a validated random-forest regressor.

    The model predicts the SIDE-SPECIFIC future gross return directly in bps.
    Live/validation costs are subtracted afterwards by the canonical decision path,
    so historical unknown spreads are never manufactured and live spread can still
    be rechecked using the fresh executable quote.
    """

    model_id: str
    version: str
    direction: str
    horizon_minutes: int
    feature_names: tuple[str, ...]
    trees: tuple[RegressionTree, ...]
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

    def _row(self, features: Mapping[str, float]) -> list[float]:
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
        return row

    def tree_predictions_bps(self, features: Mapping[str, float]) -> tuple[float, ...]:
        row = self._row(features)
        return tuple(tree.predict(row) for tree in self.trees)

    def expected_gross_return_bps(self, features: Mapping[str, float]) -> float:
        values = self.tree_predictions_bps(features)
        return sum(values) / len(values)

    def prediction_std_bps(self, features: Mapping[str, float]) -> float:
        values = self.tree_predictions_bps(features)
        if len(values) < 2:
            return 0.0
        avg = sum(values) / len(values)
        return sqrt(sum((value - avg) ** 2 for value in values) / (len(values) - 1))

    def probability_above(self, features: Mapping[str, float], threshold_bps: float) -> float:
        """Empirical tree-vote probability that gross return clears a supplied cost threshold."""
        values = self.tree_predictions_bps(features)
        return sum(1 for value in values if value > threshold_bps) / len(values)

    def to_dict(self) -> dict:
        self.validate()
        return {
            "model_type": "random_forest_direct_return_regressor",
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
                    "value_bps": list(tree.value_bps),
                }
                for tree in self.trees
            ],
            "validation_id": self.validation_id,
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> "ForestRegressorSpec":
        if payload.get("model_type") not in {None, "random_forest_direct_return_regressor"}:
            raise ForestModelError("unsupported forest model_type")
        trees = tuple(
            RegressionTree(
                children_left=tuple(int(v) for v in item["children_left"]),
                children_right=tuple(int(v) for v in item["children_right"]),
                feature=tuple(int(v) for v in item["feature"]),
                threshold=tuple(float(v) for v in item["threshold"]),
                value_bps=tuple(float(v) for v in item["value_bps"]),
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
            validation_id=str(payload["validation_id"]),
        )
        spec.validate()
        return spec


def export_random_forest_regressor(
    *,
    model,
    model_id: str,
    version: str,
    direction: str,
    horizon_minutes: int,
    feature_names: Sequence[str],
    validation_id: str,
) -> ForestRegressorSpec:
    """Export sklearn RandomForestRegressor into a standard-library runtime spec."""
    trees: list[RegressionTree] = []
    for estimator in model.estimators_:
        tree = estimator.tree_
        values: list[float] = []
        for raw in tree.value:
            # sklearn regression tree shape is typically (n_nodes, 1, 1).
            value = raw[0]
            if hasattr(value, "__len__"):
                value = value[0]
            values.append(float(value))
        trees.append(
            RegressionTree(
                children_left=tuple(int(v) for v in tree.children_left),
                children_right=tuple(int(v) for v in tree.children_right),
                feature=tuple(int(v) for v in tree.feature),
                threshold=tuple(float(v) for v in tree.threshold),
                value_bps=tuple(values),
            )
        )
    spec = ForestRegressorSpec(
        model_id=model_id,
        version=version,
        direction=direction,
        horizon_minutes=horizon_minutes,
        feature_names=tuple(feature_names),
        trees=tuple(trees),
        validation_id=validation_id,
    )
    spec.validate()
    return spec
