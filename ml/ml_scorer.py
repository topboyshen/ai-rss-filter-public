#!/usr/bin/env python3
"""
ML-based fast scorer for stage1
Falls back to LLM for borderline cases
"""
import joblib
import os

MODEL_PATH = 'ml/models/stage1_classifier.joblib'
VECTORIZER_PATH = 'ml/models/tfidf_vectorizer.joblib'

class MLScorer:
    def __init__(self):
        if os.path.exists(MODEL_PATH) and os.path.exists(VECTORIZER_PATH):
            self.clf = joblib.load(MODEL_PATH)
            self.vectorizer = joblib.load(VECTORIZER_PATH)
            self.enabled = True
        else:
            self.enabled = False
    
    def predict_score(self, title: str, summary: str = '') -> tuple[float, float]:
        if not self.enabled:
            return 5.0, 0.0
        
        text = f'{title} {summary}'
        X = self.vectorizer.transform([text])
        proba = self.clf.predict_proba(X)[0]
        confidence = max(proba)
        predicted_class = self.clf.predict(X)[0]
        score = 8.0 if predicted_class == 1 else 4.0
        return score, confidence
