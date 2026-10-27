"""Modelo local para classificar sequencias recentes de resultados HTTP."""

import random

from sklearn.ensemble import RandomForestClassifier


def features(statuses: list[int]) -> list[float]:
    errors = [int(status >= 500) for status in statuses]
    n = len(errors)
    longest = current = 0
    for error in errors:
        current = current + 1 if error else 0
        longest = max(longest, current)
    transitions = sum(left != right for left, right in zip(errors, errors[1:]))
    return [
        sum(errors) / n,
        longest / n,
        transitions / max(1, n - 1),
        float(errors[-1]),
    ]


class LocalIncidentModel:
    def __init__(self):
        randomizer = random.Random(42)
        training, labels = [], []
        for _ in range(300):
            n = randomizer.randint(6, 20)
            healthy = [200] * n
            if n >= 10 and randomizer.random() < 0.15:
                healthy[randomizer.randrange(n)] = 503
            intermittent = [503 if (index + randomizer.randint(0, 1)) % 2 else 200 for index in range(n)]
            if sum(status >= 500 for status in intermittent) in (0, n):
                intermittent = [200, 503] * (n // 2) + ([200] if n % 2 else [])
            continuous = [503] * n
            if n >= 10 and randomizer.random() < 0.15:
                continuous[randomizer.randrange(n)] = 200
            for label, sequence in (
                ("normal", healthy),
                ("intermittent", intermittent),
                ("continuous", continuous),
            ):
                training.append(features(sequence))
                labels.append(label)
        self.classifier = RandomForestClassifier(n_estimators=80, random_state=42, min_samples_leaf=2)
        self.classifier.fit(training, labels)

    def predict(self, statuses: list[int]) -> tuple[str, float]:
        probabilities = self.classifier.predict_proba([features(statuses)])[0]
        best = max(range(len(probabilities)), key=lambda index: probabilities[index])
        return str(self.classifier.classes_[best]), float(probabilities[best])
