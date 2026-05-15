# ============================================================
# 文件名：说话人分离.py
# 功能：基于 Resemblyzer 的本地说话人分离引擎
# 原理：声纹嵌入提取 → 聚类 → 标注每句话的说话人
# 依赖：Resemblyzer（谷歌开源，无需 HuggingFace 认证）
#
# 模型：Resemblyzer VoiceEncoder（~15MB，首次自动下载）
# 纯本地运行，无需任何云端 API 或 Token
# ============================================================

import os
import sys
import logging
import threading
from typing import Optional, List, Dict

import numpy as np
import librosa

logger = logging.getLogger("speaker_diarization")

# ============================================================
# 配置
# ============================================================
WINDOW_SIZE = 1.5       # 声纹分析窗口（秒）
HOP_SIZE = 0.75         # 窗口步长（秒）
SILENCE_THRESHOLD = 0.001  # 静音阈值（RMS），降低以避免过滤有效语音


class SpeakerDiarizationEngine:
    """
    说话人分离引擎（单例模式）

    使用 Resemblyzer 提取声纹嵌入，通过层次聚类分组说话人
    输入：WAV 音频文件路径
    输出：带说话人标签的分段列表

    使用示例：
        engine = SpeakerDiarizationEngine()
        segments = engine.diarize("audio.wav")
        # segments = [
        #     {"start": 0.0, "end": 2.5, "speaker": "SPEAKER_00"},
        #     {"start": 2.8, "end": 5.1, "speaker": "SPEAKER_01"},
        # ]
    """

    _instance: Optional["SpeakerDiarizationEngine"] = None

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized") and self._initialized:
            return
        self._initialized = True

        self._encoder = None
        self._model_loaded = False
        self._lock = threading.Lock()

    # ============================================================
    # 公开接口
    # ============================================================

    def get_status(self) -> dict:
        """获取引擎状态"""
        return {
            "model_loaded": self._model_loaded,
            "model_name": "Resemblyzer VoiceEncoder",
            "model_size": "~15MB",
            "provider": "Resemblyzer（谷歌开源声纹编码，纯本地运行）"
        }

    def load_model(self) -> bool:
        """
        加载声纹编码模型

        首次运行自动下载（~15MB），后续使用缓存
        """
        if self._model_loaded:
            return True

        try:
            from resemblyzer import VoiceEncoder

            logger.info("正在加载声纹编码模型（Resemblyzer）...")
            self._encoder = VoiceEncoder()
            self._model_loaded = True
            logger.info("声纹编码模型加载完成")
            return True

        except Exception as e:
            logger.error(f"声纹模型加载失败：{e}")
            return False

    def diarize(
        self,
        audio_path: str,
        n_speakers: int = None
    ) -> List[Dict]:
        """
        对音频文件进行说话人分离

        参数：
            audio_path：WAV 音频文件路径
            n_speakers：预期说话人数量（None 则自动估计）

        返回：
            list[dict]：说话人分段列表
        """
        if not os.path.exists(audio_path):
            logger.error(f"音频文件不存在：{audio_path}")
            return []

        wav, sr = librosa.load(audio_path, sr=16000)
        return self._diarize_waveform(wav, sr, n_speakers)

    def diarize_from_buffer(self, wav_buffer) -> List[Dict]:
        """
        从内存中的 WAV 数据进行说话人分离（用于实时场景）

        参数：
            wav_buffer：BytesIO 对象，包含 WAV 格式音频数据

        返回：
            list[dict]：说话人分段列表
        """
        wav, sr = librosa.load(wav_buffer, sr=16000)
        return self._diarize_waveform(wav, sr)

    def _diarize_waveform(
        self,
        wav: "np.ndarray",
        sr: int,
        n_speakers: int = None
    ) -> List[Dict]:
        """核心声纹聚类逻辑"""
        if not self._model_loaded:
            if not self.load_model():
                return []

        try:
            from sklearn.cluster import AgglomerativeClustering

            duration = len(wav) / sr
            logger.info(f"音频时长：{duration:.1f}s")

            # 按窗口切分，提取声纹嵌入
            window_samples = int(WINDOW_SIZE * sr)
            hop_samples = int(HOP_SIZE * sr)

            embeddings = []
            intervals = []

            for start in range(0, len(wav) - window_samples, hop_samples):
                end = start + window_samples
                chunk = wav[start:end]

                # 跳过静音段
                rms = np.sqrt(np.mean(chunk ** 2))
                if rms < SILENCE_THRESHOLD:
                    continue

                emb = self._encoder.embed_utterance(chunk)
                embeddings.append(emb)
                intervals.append((start / sr, end / sr))

            if len(embeddings) < 2:
                logger.warning("有效语音段不足，视为单人发言")
                return [{
                    "start": 0.0,
                    "end": round(duration, 2),
                    "speaker": "SPEAKER_00"
                }]

            embeddings = np.array(embeddings)
            logger.info(f"提取了 {len(embeddings)} 个声纹嵌入")

            # 自动估计说话人数量
            if n_speakers is None:
                n_speakers = self._estimate_speakers(embeddings)

            # 层次聚类
            clustering = AgglomerativeClustering(
                n_clusters=n_speakers
            ).fit(embeddings)
            labels = clustering.labels_
            labels = self._smooth_labels(labels)

            # 合并相邻同说话人片段
            segments = self._merge_segments(labels, intervals)
            segments = self._merge_short_segments(segments)

            logger.info(
                f"说话人分离完成：{len(segments)} 个分段，"
                f"{n_speakers} 位说话人"
            )

            return segments

        except Exception as e:
            logger.error(f"说话人分离失败：{e}")
            import traceback
            traceback.print_exc()
            return []

    def merge_with_transcript(
        self,
        segments: List[Dict],
        transcript_text: str,
        transcript_segments: List[Dict] = None
    ) -> str:
        """
        将说话人分离结果与转写文本合并

        参数：
            segments：说话人分离分段列表
            transcript_text：完整转写文本
            transcript_segments：ASR 分句列表（可选，含时间戳）

        返回：
            str：带说话人标签的格式化文本
        """
        if not segments:
            return transcript_text

        # 检查 ASR 分句是否有有效时间戳
        has_timestamps = (
            transcript_segments
            and len(transcript_segments) > 0
            and any(
                s.get("start_time", 0) > 0 or s.get("end_time", 0) > 0
                for s in transcript_segments
            )
        )

        if has_timestamps:
            return self._merge_with_timestamps(segments, transcript_segments)

        return self._merge_proportional(segments, transcript_text)

    # ============================================================
    # 内部方法
    # ============================================================

    def _estimate_speakers(self, embeddings: np.ndarray) -> int:
        """用轮廓系数自动检测最优说话人数量"""
        from sklearn.cluster import AgglomerativeClustering
        from sklearn.metrics import silhouette_score

        n_samples = len(embeddings)
        if n_samples < 4:
            return min(2, n_samples)

        max_clusters = min(6, n_samples - 1)
        best_score = -1
        best_n = 2

        for n in range(2, max_clusters + 1):
            try:
                clustering = AgglomerativeClustering(
                    n_clusters=n
                ).fit(embeddings)
                labels = clustering.labels_
                if len(set(labels)) < 2:
                    continue
                score = silhouette_score(
                    embeddings, labels, metric="cosine"
                )
                if score > best_score:
                    best_score = score
                    best_n = n
            except Exception:
                continue

        logger.info(
            f"自动检测说话人数量：{best_n} 人"
            f"（轮廓系数={best_score:.3f}）"
        )
        return best_n

    def _smooth_labels(self, labels: np.ndarray) -> np.ndarray:
        """
        平滑短时声纹抖动。

        实时场景下 1.5 秒窗口容易把同一人的连续讲话切成 A/B/A，
        这里把夹在相同说话人之间的孤立窗口合并回去。
        """
        if len(labels) < 3:
            return labels

        smoothed = labels.copy()
        for i in range(1, len(smoothed) - 1):
            if smoothed[i - 1] == smoothed[i + 1] and smoothed[i] != smoothed[i - 1]:
                smoothed[i] = smoothed[i - 1]
        return smoothed

    def _merge_short_segments(self, segments: List[Dict]) -> List[Dict]:
        """
        合并过短的孤立片段，提升实时展示稳定性。

        真实会议里同一人连续说话常超过 5 秒，Resemblyzer 在短窗口上可能
        产生 1~2 秒的误切。将这类短片段并入相邻较长片段，避免频繁跳 speaker。
        """
        if len(segments) < 3:
            return segments

        merged = [dict(s) for s in segments]
        i = 1
        while i < len(merged) - 1:
            current = merged[i]
            duration = current["end"] - current["start"]
            prev_seg = merged[i - 1]
            next_seg = merged[i + 1]

            if duration <= 2.25:
                if prev_seg["speaker"] == next_seg["speaker"]:
                    prev_seg["end"] = next_seg["end"]
                    merged.pop(i + 1)
                    merged.pop(i)
                    continue

                prev_duration = prev_seg["end"] - prev_seg["start"]
                next_duration = next_seg["end"] - next_seg["start"]
                target = prev_seg if prev_duration >= next_duration else next_seg
                current["speaker"] = target["speaker"]

            i += 1

        # 再做一次相邻同说话人合并
        compact = []
        for seg in merged:
            if compact and compact[-1]["speaker"] == seg["speaker"]:
                compact[-1]["end"] = seg["end"]
            else:
                compact.append(seg)
        return compact

    def _merge_segments(
        self,
        labels: np.ndarray,
        intervals: List[tuple]
    ) -> List[Dict]:
        """合并相邻同说话人片段"""
        if len(labels) == 0:
            return []

        segments = []
        current_label = labels[0]
        current_start = intervals[0][0]
        current_end = intervals[0][1]

        for i in range(1, len(labels)):
            if labels[i] != current_label:
                segments.append({
                    "start": round(current_start, 2),
                    "end": round(current_end, 2),
                    "speaker": f"SPEAKER_{current_label:02d}"
                })
                current_label = labels[i]
                current_start = intervals[i][0]
            current_end = intervals[i][1]

        segments.append({
            "start": round(current_start, 2),
            "end": round(current_end, 2),
            "speaker": f"SPEAKER_{current_label:02d}"
        })

        return segments

    def _merge_with_timestamps(
        self,
        diarization_segments: List[Dict],
        asr_segments: List[Dict]
    ) -> str:
        """基于时间戳精确匹配说话人和转写文本"""
        lines = []
        speaker_labels = sorted(set(s["speaker"] for s in diarization_segments))
        speaker_map = {
            label: f"发言人 {chr(65 + i)}"
            for i, label in enumerate(speaker_labels)
        }

        for asr_seg in asr_segments:
            seg_start = asr_seg.get("start_time", 0)
            seg_end = asr_seg.get("end_time", 0)
            seg_text = asr_seg.get("text", "").strip()
            if not seg_text:
                continue

            best_speaker = None
            max_overlap = 0
            for ds in diarization_segments:
                overlap_start = max(seg_start, ds["start"])
                overlap_end = min(seg_end, ds["end"])
                overlap = overlap_end - overlap_start
                if overlap > max_overlap:
                    max_overlap = overlap
                    best_speaker = ds["speaker"]

            speaker_name = speaker_map.get(best_speaker, "发言人 ?")
            lines.append(f"[{speaker_name}] {seg_text}")

        return "\n\n".join(lines)

    def _merge_proportional(
        self,
        segments: List[Dict],
        transcript_text: str
    ) -> str:
        """按时间比例分配说话人（无精确时间戳时的降级方案）"""
        if not transcript_text.strip():
            return ""

        # 用多种标点分句
        import re
        raw_sentences = re.split(r'[。！？；\n]+', transcript_text)
        sentences = [s.strip() for s in raw_sentences if s.strip()]

        if not sentences:
            return transcript_text

        # 说话人标签映射
        speaker_labels = sorted(set(s["speaker"] for s in segments))
        speaker_map = {
            label: f"发言人 {chr(65 + i)}"
            for i, label in enumerate(speaker_labels)
        }

        # 计算每个说话人的时间占比
        total_time = sum(s["end"] - s["start"] for s in segments)
        if total_time <= 0:
            return transcript_text

        speaker_ratio = {}
        for s in segments:
            speaker = s["speaker"]
            duration = s["end"] - s["start"]
            speaker_ratio[speaker] = speaker_ratio.get(speaker, 0) + duration / total_time

        # 按时间顺序排列说话人（用于交替分配）
        ordered_speakers = []
        for s in segments:
            if s["speaker"] not in ordered_speakers:
                ordered_speakers.append(s["speaker"])

        # 按比例分配句子数
        total_sentences = len(sentences)
        speaker_sentence_counts = {}
        for speaker, ratio in speaker_ratio.items():
            speaker_sentence_counts[speaker] = max(1, round(total_sentences * ratio))

        # 调整使总数匹配
        diff = total_sentences - sum(speaker_sentence_counts.values())
        if diff != 0 and ordered_speakers:
            # 把差额分配给占比最大的说话人
            main_speaker = max(speaker_ratio, key=speaker_ratio.get)
            speaker_sentence_counts[main_speaker] += diff

        # 按时间顺序交替分配句子
        lines = []
        sentence_idx = 0
        round_idx = 0
        max_rounds = max(
            (count for count in speaker_sentence_counts.values()),
            default=1
        )

        while sentence_idx < total_sentences:
            for speaker in ordered_speakers:
                count = speaker_sentence_counts.get(speaker, 0)
                # 每轮分配 1~2 句
                per_round = max(1, min(2, count // max_rounds)) if max_rounds > 0 else 1
                for _ in range(per_round):
                    if sentence_idx >= total_sentences:
                        break
                    lines.append(
                        f"[{speaker_map[speaker]}] {sentences[sentence_idx]}"
                    )
                    sentence_idx += 1
            round_idx += 1
            if round_idx > 100:  # 安全上限
                break

        return "\n\n".join(lines)


def get_speaker_diarization_engine() -> SpeakerDiarizationEngine:
    """获取全局唯一的说话人分离引擎实例"""
    return SpeakerDiarizationEngine()
