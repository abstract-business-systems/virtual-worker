"""Small Pipecat measurement adapters; do not infer missing provider usage."""
import os

from pipecat.frames.frames import (
    ErrorFrame, UserStartedSpeakingFrame, UserStoppedSpeakingFrame,
    VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameProcessor, FrameDirection


def realtime_user_params():
    """Pin the stop policy: the framework default smart-turn path can wait for
    its five-second fallback when a direct-audio service has no transcripts.
    VAD already supplies the pause tolerance; do not add a second timeout.
    """
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams
    from pipecat.processors.aggregators.llm_response_universal import LLMUserAggregatorParams
    from pipecat.turns.user_turn_strategies import UserTurnStrategies
    from pipecat.turns.user_stop import SpeechTimeoutUserTurnStopStrategy
    from poc_report import endpoint_silence
    return LLMUserAggregatorParams(
        vad_analyzer=SileroVADAnalyzer(params=VADParams(
            min_volume=float(os.getenv('CHAT_MIN_VOLUME', '.15')),
            start_secs=.2, stop_secs=endpoint_silence())),
        user_turn_strategies=UserTurnStrategies(stop=[SpeechTimeoutUserTurnStopStrategy(
            user_speech_timeout=0.0, wait_for_transcript=False)]),
    )


class SessionBoundary(FrameProcessor):
    def __init__(self, session, *, vad_only=False):
        super().__init__()
        self.session, self.vad_only = session, vad_only

    async def process_frame(self, frame, direction):
        await super().process_frame(frame, direction)
        if direction == FrameDirection.DOWNSTREAM:
            start = VADUserStartedSpeakingFrame if self.vad_only else UserStartedSpeakingFrame
            stop = VADUserStoppedSpeakingFrame if self.vad_only else UserStoppedSpeakingFrame
            if isinstance(frame, start) and not (os.getenv('CHAT_TEST_PLAN') or os.getenv('CHAT_TEST_WAV')):
                self.session.next_input()
            if isinstance(frame, stop):
                self.session.endpoint()
            if isinstance(frame, ErrorFrame):
                self.session.error(str(frame))
        await self.push_frame(frame, direction)
