/**
 * Energy-based end-of-speech detector for the hands-free voice page.
 *
 * Fed one loudness sample (RMS, 0..1) per animation frame. It learns the room
 * noise during the first moments, waits for the user to start speaking, and
 * reports when they have been quiet long enough for the turn to be over.
 */

export type VadVerdict = 'continue' | 'send' | 'timeout';

export interface VadOptions {
  /** Time spent learning the room noise before speech can be detected (ms). */
  calibrationMs?: number;
  /** Loudness must exceed the noise floor by this factor to count as speech. */
  speechFactor?: number;
  /** Absolute floor for the speech threshold, so a silent room is not hair-triggered. */
  minThreshold?: number;
  /** Speech must be sustained this long before the turn is considered started (ms). */
  onsetMs?: number;
  /** Silence after speech that ends the turn (ms). */
  silenceMs?: number;
  /** Give up when nobody speaks for this long (ms). */
  noSpeechTimeoutMs?: number;
  /** Hard cap on one utterance (ms). */
  maxUtteranceMs?: number;
}

const DEFAULTS: Required<VadOptions> = {
  calibrationMs: 300,
  speechFactor: 3,
  minThreshold: 0.02,
  onsetMs: 90,
  silenceMs: 1200,
  noSpeechTimeoutMs: 8000,
  maxUtteranceMs: 30000,
};

export class VoiceActivityDetector {
  private readonly opts: Required<VadOptions>;
  private startedAt: number | null = null;
  private noise = 0;
  private noiseSamples = 0;
  private loudSince: number | null = null;
  private speechStartedAt: number | null = null;
  private lastVoiceAt = 0;

  constructor(options: VadOptions = {}) {
    this.opts = { ...DEFAULTS, ...options };
  }

  /** Speech threshold currently in force. */
  get threshold(): number {
    return Math.max(this.opts.minThreshold, this.noise * this.opts.speechFactor);
  }

  /** True once the user has actually started talking. */
  get speaking(): boolean {
    return this.speechStartedAt !== null;
  }

  step(rms: number, now: number): VadVerdict {
    if (this.startedAt === null) this.startedAt = now;
    const elapsed = now - this.startedAt;

    if (elapsed < this.opts.calibrationMs) {
      this.noiseSamples += 1;
      this.noise += (rms - this.noise) / this.noiseSamples;
      return 'continue';
    }

    const loud = rms >= this.threshold;

    if (this.speechStartedAt === null) {
      if (loud) {
        if (this.loudSince === null) this.loudSince = now;
        if (now - this.loudSince >= this.opts.onsetMs) {
          this.speechStartedAt = this.loudSince;
          this.lastVoiceAt = now;
        }
      } else {
        this.loudSince = null;
        // Keep tracking slow drifts in room noise while we wait.
        this.noise = 0.95 * this.noise + 0.05 * rms;
      }
      if (this.speechStartedAt === null && elapsed >= this.opts.noSpeechTimeoutMs) {
        return 'timeout';
      }
      return 'continue';
    }

    if (loud) this.lastVoiceAt = now;
    if (now - this.lastVoiceAt >= this.opts.silenceMs) return 'send';
    if (now - this.speechStartedAt >= this.opts.maxUtteranceMs) return 'send';
    return 'continue';
  }
}
