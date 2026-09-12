"""V3 voice layer — state machine and orchestration for the wake→listen→
process→speak→(interrupt) interaction, built on small, testable components in
perception/ (microphone, wakeword, endpointer, stt, tts, audio_player)."""
