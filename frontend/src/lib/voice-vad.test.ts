import { describe, expect, it } from 'vitest';
import { VoiceActivityDetector, type VadVerdict } from './voice-vad';

const FRAME = 16; // ms, one animation frame

/** Feed `ms` of constant loudness; return the first non-continue verdict, if any. */
function feed(
  vad: VoiceActivityDetector,
  clock: { t: number },
  rms: number,
  ms: number,
): VadVerdict {
  for (let spent = 0; spent < ms; spent += FRAME) {
    const verdict = vad.step(rms, clock.t);
    clock.t += FRAME;
    if (verdict !== 'continue') return verdict;
  }
  return 'continue';
}

describe('VoiceActivityDetector', () => {
  it('sends after speech followed by enough silence', () => {
    const vad = new VoiceActivityDetector();
    const clock = { t: 1000 };
    expect(feed(vad, clock, 0.005, 400)).toBe('continue'); // room noise
    expect(feed(vad, clock, 0.2, 800)).toBe('continue'); // talking
    expect(vad.speaking).toBe(true);
    expect(feed(vad, clock, 0.005, 1000)).toBe('continue'); // short pause
    expect(feed(vad, clock, 0.005, 400)).toBe('send'); // pause exceeds 1.2 s
  });

  it('does not end the turn on a pause between words', () => {
    const vad = new VoiceActivityDetector();
    const clock = { t: 0 };
    feed(vad, clock, 0.005, 400);
    feed(vad, clock, 0.2, 500);
    expect(feed(vad, clock, 0.005, 700)).toBe('continue');
    expect(feed(vad, clock, 0.2, 500)).toBe('continue');
    expect(feed(vad, clock, 0.005, 700)).toBe('continue');
  });

  it('times out when nobody speaks', () => {
    const vad = new VoiceActivityDetector({ noSpeechTimeoutMs: 2000 });
    const clock = { t: 0 };
    expect(feed(vad, clock, 0.005, 1900)).toBe('continue');
    expect(feed(vad, clock, 0.005, 300)).toBe('timeout');
    expect(vad.speaking).toBe(false);
  });

  it('ignores a click too short to be speech', () => {
    const vad = new VoiceActivityDetector();
    const clock = { t: 0 };
    feed(vad, clock, 0.005, 400);
    feed(vad, clock, 0.5, 32); // two frames only, below the 90 ms onset
    feed(vad, clock, 0.005, 200);
    expect(vad.speaking).toBe(false);
  });

  it('raises its threshold in a noisy room', () => {
    const vad = new VoiceActivityDetector();
    const clock = { t: 0 };
    feed(vad, clock, 0.05, 400); // loud fan
    expect(vad.threshold).toBeCloseTo(0.15, 2);
    feed(vad, clock, 0.1, 500); // louder than the fan, but not speech-loud
    expect(vad.speaking).toBe(false);
    feed(vad, clock, 0.3, 300);
    expect(vad.speaking).toBe(true);
  });

  it('caps a never-ending utterance', () => {
    const vad = new VoiceActivityDetector({ maxUtteranceMs: 3000 });
    const clock = { t: 0 };
    feed(vad, clock, 0.005, 400);
    expect(feed(vad, clock, 0.2, 2800)).toBe('continue');
    expect(feed(vad, clock, 0.2, 600)).toBe('send');
  });
});
