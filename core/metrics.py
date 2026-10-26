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

    def get_prometheus_metrics(self) -> str:
        """生成 Prometheus 格式的 metrics"""
        with self.lock:
            lines = []

            # 添加 HELP 和 TYPE
            lines.append("# HELP ai_filter_uptime_seconds Uptime in seconds")
            lines.append("# TYPE ai_filter_uptime_seconds gauge")
            lines.append(f"ai_filter_uptime_seconds {time.time() - self.start_time:.2f}")
            lines.append("")

            # LLM API metrics
            lines.append("# HELP ai_filter_llm_calls_total Total LLM API calls")
            lines.append("# TYPE ai_filter_llm_calls_total counter")
            for backend, count in self.llm_calls_total.items():
                lines.append(f'ai_filter_llm_calls_total{{backend="{backend}"}} {count}')
            lines.append("")

            lines.append("# HELP ai_filter_llm_calls_success Successful LLM API calls")
            lines.append("# TYPE ai_filter_llm_calls_success counter")
            for backend, count in self.llm_calls_success.items():
                lines.append(f'ai_filter_llm_calls_success{{backend="{backend}"}} {count}')
            lines.append("")

            lines.append("# HELP ai_filter_llm_calls_failed Failed LLM API calls")
            lines.append("# TYPE ai_filter_llm_calls_failed counter")
            for backend, count in self.llm_calls_failed.items():
                lines.append(f'ai_filter_llm_calls_failed{{backend="{backend}"}} {count}')
            lines.append("")

            lines.append("# HELP ai_filter_llm_latency_avg_ms Average LLM API latency in milliseconds")
            lines.append("# TYPE ai_filter_llm_latency_avg_ms gauge")
            for backend in self.llm_latency_sum.keys():
                if self.llm_latency_count[backend] > 0:
                    avg = self.llm_latency_sum[backend] / self.llm_latency_count[backend]
                    lines.append(f'ai_filter_llm_latency_avg_ms{{backend="{backend}"}} {avg:.2f}')
            lines.append("")

            lines.append("# HELP ai_filter_llm_tokens_input_total Total input tokens")
            lines.append("# TYPE ai_filter_llm_tokens_input_total counter")
            for backend, count in self.llm_tokens_input.items():
                lines.append(f'ai_filter_llm_tokens_input_total{{backend="{backend}"}} {count}')
            lines.append("")

            lines.append("# HELP ai_filter_llm_tokens_output_total Total output tokens")
            lines.append("# TYPE ai_filter_llm_tokens_output_total counter")
            for backend, count in self.llm_tokens_output.items():
                lines.append(f'ai_filter_llm_tokens_output_total{{backend="{backend}"}} {count}')
            lines.append("")

            # Scoring metrics
            lines.append("# HELP ai_filter_score_stage1_total Total stage1 scoring")
            lines.append("# TYPE ai_filter_score_stage1_total counter")
            lines.append(f"ai_filter_score_stage1_total {self.score_stage1_total}")
            lines.append("")

            lines.append("# HELP ai_filter_score_stage1_local_fallback Stage1 local fallback count")
            lines.append("# TYPE ai_filter_score_stage1_local_fallback counter")
            lines.append(f"ai_filter_score_stage1_local_fallback {self.score_stage1_local_fallback}")
            lines.append("")

            lines.append("# HELP ai_filter_score_stage2_total Total stage2 scoring")
            lines.append("# TYPE ai_filter_score_stage2_total counter")
            lines.append(f"ai_filter_score_stage2_total {self.score_stage2_total}")
            lines.append("")

            lines.append("# HELP ai_filter_fast_pass_hits Fast pass hits by tier")
            lines.append("# TYPE ai_filter_fast_pass_hits counter")
            for tier, count in self.fast_pass_hits.items():
                lines.append(f'ai_filter_fast_pass_hits{{tier="{tier}"}} {count}')
            lines.append("")

            # Feed metrics
            lines.append("# HELP ai_filter_feed_refresh_total Total feed refreshes")
            lines.append("# TYPE ai_filter_feed_refresh_total counter")
            for feed, count in self.feed_refresh_total.items():
                lines.append(f'ai_filter_feed_refresh_total{{feed="{feed}"}} {count}')
            lines.append("")

            lines.append("# HELP ai_filter_feed_refresh_errors Feed refresh errors")
            lines.append("# TYPE ai_filter_feed_refresh_errors counter")
            for feed, count in self.feed_refresh_errors.items():
                lines.append(f'ai_filter_feed_refresh_errors{{feed="{feed}"}} {count}')
            lines.append("")

            lines.append("# HELP ai_filter_feed_articles_processed_total Total articles processed")
            lines.append("# TYPE ai_filter_feed_articles_processed_total counter")
            for feed, count in self.feed_articles_processed.items():
                lines.append(f'ai_filter_feed_articles_processed_total{{feed="{feed}"}} {count}')
            lines.append("")

            lines.append("# HELP ai_filter_feed_articles_passed_total Total articles passed")
            lines.append("# TYPE ai_filter_feed_articles_passed_total counter")
            for feed, count in self.feed_articles_passed.items():
                lines.append(f'ai_filter_feed_articles_passed_total{{feed="{feed}"}} {count}')
            lines.append("")

            # Cache metrics
            lines.append("# HELP ai_filter_cache_hits_total Cache hits")
            lines.append("# TYPE ai_filter_cache_hits_total counter")
            lines.append(f"ai_filter_cache_hits_total {self.cache_hits}")
            lines.append("")

            lines.append("# HELP ai_filter_cache_misses_total Cache misses")
            lines.append("# TYPE ai_filter_cache_misses_total counter")
            lines.append(f"ai_filter_cache_misses_total {self.cache_misses}")
            lines.append("")

            # Database metrics
            lines.append("# HELP ai_filter_db_locks_total Database lock occurrences")
            lines.append("# TYPE ai_filter_db_locks_total counter")
            lines.append(f"ai_filter_db_locks_total {self.db_locks}")
            lines.append("")

            return "\n".join(lines)


# 全局实例
_metrics = MetricsCollector()


def get_metrics() -> MetricsCollector:
    """获取全局 metrics 收集器"""
    return _metrics
