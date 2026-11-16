"""
Prometheus metrics for ai-rss-filter
监控 LLM API 调用、评分性能、Feed 处理状态
"""
import time
from collections import defaultdict
from threading import Lock
import logging

logger = logging.getLogger(__name__)


class MetricsCollector:
    """线程安全的 Prometheus 风格 metrics 收集器"""

    def __init__(self):
        self.lock = Lock()

        # LLM API metrics
        self.llm_calls_total = defaultdict(int)  # {backend_name: count}
        self.llm_calls_success = defaultdict(int)
        self.llm_calls_failed = defaultdict(int)
        self.llm_latency_sum = defaultdict(float)  # {backend_name: total_ms}
        self.llm_latency_count = defaultdict(int)
        self.llm_tokens_input = defaultdict(int)
        self.llm_tokens_output = defaultdict(int)

        # Scoring metrics
        self.score_stage1_total = 0
        self.score_stage1_local_fallback = 0
        self.score_stage2_total = 0
        self.fast_pass_hits = defaultdict(int)  # {tier_name: count}

        # Feed metrics
        self.feed_refresh_total = defaultdict(int)  # {feed_name: count}
        self.feed_refresh_errors = defaultdict(int)
        self.feed_articles_processed = defaultdict(int)
        self.feed_articles_passed = defaultdict(int)

        # Cache metrics
        self.cache_hits = 0
        self.cache_misses = 0

        # Database metrics
        self.db_locks = 0

        # Start time
        self.start_time = time.time()

    def record_llm_call(self, backend_name: str, success: bool, latency_ms: float,
                       input_tokens: int = 0, output_tokens: int = 0):
        """记录 LLM API 调用"""
        with self.lock:
            self.llm_calls_total[backend_name] += 1
            if success:
                self.llm_calls_success[backend_name] += 1
                self.llm_latency_sum[backend_name] += latency_ms
                self.llm_latency_count[backend_name] += 1
                self.llm_tokens_input[backend_name] += input_tokens
                self.llm_tokens_output[backend_name] += output_tokens
            else:
                self.llm_calls_failed[backend_name] += 1

    def record_score_stage1(self, used_local_fallback: bool = False):
        """记录 stage1 评分"""
        with self.lock:
            self.score_stage1_total += 1
            if used_local_fallback:
                self.score_stage1_local_fallback += 1

    def record_score_stage2(self):
        """记录 stage2 评分"""
        with self.lock:
            self.score_stage2_total += 1

    def record_fast_pass(self, tier_name: str):
        """记录 fast pass 命中"""
        with self.lock:
            self.fast_pass_hits[tier_name] += 1

    def record_feed_refresh(self, feed_name: str, error: bool = False,
                           articles_processed: int = 0, articles_passed: int = 0):
        """记录 Feed 刷新"""
        with self.lock:
            self.feed_refresh_total[feed_name] += 1
            if error:
                self.feed_refresh_errors[feed_name] += 1
            self.feed_articles_processed[feed_name] += articles_processed
            self.feed_articles_passed[feed_name] += articles_passed

    def record_cache(self, hit: bool):
        """记录缓存命中/未命中"""
        with self.lock:
            if hit:
                self.cache_hits += 1
            else:
                self.cache_misses += 1

    def record_db_lock(self):
        """记录数据库锁定"""
        with self.lock:
            self.db_locks += 1


# 全局实例
_metrics = MetricsCollector()


def get_metrics() -> MetricsCollector:
    """获取全局 metrics 收集器"""
    return _metrics
