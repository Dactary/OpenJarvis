import { useCallback, useEffect, useRef, useState } from 'react';
import { apiFetch, fetchModels, fetchServerInfo, synthesizeSpeech } from '../lib/api';
import { stripThinkTags, toSpeakableText } from '../lib/message-text';
import { useAppStore } from '../lib/store';
import { isWhisperHallucination } from '../lib/voice-hallucinations';
import { VoiceActivityDetector } from '../lib/voice-vad';

/**
 * Hands-free voice page: the OpenJarvis logo sits in the middle of the screen
 * and reacts to what is happening -- it listens (ring follows the microphone),
 * thinks (rotating arc) and speaks (ring follows the synthesized voice).
 *
 * One turn = record until silence -> /v1/speech/transcribe -> /v1/chat/completions
 * -> /v1/speech/synthesize -> playback. Everything stays on the local server.
 */

type Phase = 'idle' | 'listening' | 'thinking' | 'speaking';

interface Turn {
  role: 'user' | 'assistant';
  content: string;
}

/**
 * Character presets prepended to the system prompt.
 *
 * Adapted from core/personnalite.py of sosoj92/jarvis-assistant-vocal
 * (https://github.com/sosoj92/jarvis-assistant-vocal), MIT License,
 * Copyright (c) 2026 sosoj92.
 */
type Persona = 'jarvis' | 'neutre' | 'concis';
const PERSONA_KEY = 'openjarvis-voice-persona';
const PERSONAS: Record<Persona, { label: string; prompt: string }> = {
  jarvis: {
    label: 'Jarvis',
    prompt:
      "Tu es Jarvis, l'assistant de Tony Stark : poli, distingué, légèrement " +
      "britannique, avec un humour pince-sans-rire et un sarcasme affectueux très " +
      "discret. Tu t'adresses à l'utilisateur avec élégance mais restes toujours " +
      "efficace et utile — l'esprit avant tout, jamais lourd ni impoli.",
  },
  neutre: {
    label: 'Neutre',
    prompt: 'Tu es Jarvis, un assistant neutre, factuel et serviable, sans fioritures.',
  },
  concis: {
    label: 'Concis',
    prompt:
      "Tu es Jarvis, un assistant extrêmement concis : tu vas droit au but, " +
      "idéalement en une phrase, sans formule de politesse superflue.",
  },
};

function readPersona(): Persona {
  try {
    const saved = localStorage.getItem(PERSONA_KEY);
    return saved === 'neutre' || saved === 'concis' ? saved : 'jarvis';
  } catch {
    return 'jarvis';
  }
}

/** What every persona must respect because the answer is spoken, not read. */
const VOICE_RULES =
  "Tes réponses sont lues à voix haute : réponds brièvement, en phrases " +
  "naturelles, sans markdown, sans listes ni emoji, dans la langue de " +
  "l'utilisateur. Ce que dit l'utilisateur est transcrit automatiquement : ton " +
  "nom peut y apparaître déformé (Jarvi, Jarvie, Jarvice), et si une phrase n'a " +
  "aucun sens, demande simplement de répéter. Pour l'heure, la date, les " +
  "minuteurs, les notes, la météo, un calcul ou un souvenir à retenir, utilise " +
  "tes outils au lieu de deviner, puis donne le résultat en une phrase.";

/** What happens once Jarvis has finished answering. */
type AfterReply = 'wake' | 'follow' | 'continuous';
const AFTER_KEY = 'openjarvis-voice-after-reply';
/** Follow-up window: long enough to react, short enough not to eavesdrop. */
const FOLLOW_UP_MS = 6000;
/** Follow-ups allowed before "Hey Jarvis" is required again. */
const MAX_FOLLOW_UPS = 2;

function readAfterReply(): AfterReply {
  try {
    const saved = localStorage.getItem(AFTER_KEY);
    return saved === 'wake' || saved === 'continuous' ? saved : 'follow';
  } catch {
    return 'follow';
  }
}

const STATUS: Record<Phase, string> = {
  idle: 'Dites « Hey Jarvis », cliquez sur le réacteur ou appuyez sur Espace',
  listening: 'Je vous écoute…',
  thinking: 'Je réfléchis…',
  speaking: 'Je vous réponds…',
};

type Visual = 'reactor' | 'logo';
const VISUAL_KEY = 'openjarvis-voice-visual';

/** One colour per state, as in the reactor HUD this drawing is adapted from. */
const REACTOR_COLORS: Record<Phase, { r: number; g: number; b: number }> = {
  idle: { r: 42, g: 112, b: 130 },
  listening: { r: 46, g: 224, b: 255 },
  thinking: { r: 138, g: 108, b: 255 },
  speaking: { r: 255, g: 176, b: 62 },
};
const REACTOR_COILS = 10;
const REACTOR_TICKS = 72;

function roundedRect(
  g: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
): void {
  g.beginPath();
  g.moveTo(x + r, y);
  g.arcTo(x + w, y, x + w, y + h, r);
  g.arcTo(x + w, y + h, x, y + h, r);
  g.arcTo(x, y + h, x, y, r);
  g.arcTo(x, y, x + w, y, r);
  g.closePath();
}

/**
 * Arc-reactor visual: halo, concentric rings, a rotating tick reticle, ten
 * orbiting coils, a rotating triangle and a core that swells with the voice.
 *
 * Adapted from hud.html of sosoj92/jarvis-assistant-vocal
 * (https://github.com/sosoj92/jarvis-assistant-vocal), MIT License,
 * Copyright (c) 2026 sosoj92.
 */
function drawReactor(
  g: CanvasRenderingContext2D,
  size: number,
  t: number,
  level: number,
  color: { r: number; g: number; b: number },
  thinking: boolean,
): void {
  const rgba = (a: number) =>
    `rgba(${Math.round(color.r)},${Math.round(color.g)},${Math.round(color.b)},${a})`;
  const cx = size / 2;
  const cy = size / 2;
  const R = size * 0.46;
  const breath = 0.5 + 0.5 * Math.sin(t * 0.0018);
  const speed = thinking ? 4 : 1; // the machinery spins up while it thinks
  const rotTicks = t * 0.00018 * speed;
  const rotCoils = -t * 0.00007 * speed;
  const rotTriangle = t * 0.00011 * speed;

  g.lineCap = 'round';

  const halo = g.createRadialGradient(cx, cy, R * 0.2, cx, cy, R * 1.08);
  halo.addColorStop(0, rgba(0.1 + level * 0.1));
  halo.addColorStop(1, rgba(0));
  g.fillStyle = halo;
  g.beginPath();
  g.arc(cx, cy, R * 1.08, 0, Math.PI * 2);
  g.fill();

  [1.0, 0.9, 0.62, 0.4].forEach((f, i) => {
    g.beginPath();
    g.arc(cx, cy, R * f, 0, Math.PI * 2);
    g.strokeStyle = rgba(i === 0 ? 0.35 : 0.22);
    g.lineWidth = i === 0 ? 2 : 1.2;
    g.stroke();
  });

  g.save();
  g.translate(cx, cy);
  g.rotate(rotTicks);
  const scale = Math.min(1, R / 200);
  for (let i = 0; i < REACTOR_TICKS; i += 1) {
    const a = (i / REACTOR_TICKS) * Math.PI * 2;
    const long = i % 6 === 0;
    const r1 = R * 0.78;
    const r2 = r1 + (long ? 14 : 7) * scale;
    g.beginPath();
    g.moveTo(Math.cos(a) * r1, Math.sin(a) * r1);
    g.lineTo(Math.cos(a) * r2, Math.sin(a) * r2);
    g.strokeStyle = rgba(long ? 0.7 : 0.28);
    g.lineWidth = long ? 1.8 : 1;
    g.stroke();
  }
  g.restore();

  const rc = R * 0.52;
  const bw = R * 0.13;
  const bh = R * 0.2;
  for (let i = 0; i < REACTOR_COILS; i += 1) {
    const a = (i / REACTOR_COILS) * Math.PI * 2 + rotCoils;
    g.save();
    g.translate(cx + Math.cos(a) * rc, cy + Math.sin(a) * rc);
    g.rotate(a + Math.PI / 2);
    roundedRect(g, -bw / 2, -bh / 2, bw, bh, 4);
    g.fillStyle = rgba(0.08);
    g.fill();
    g.strokeStyle = rgba(0.6);
    g.lineWidth = 1.4;
    g.stroke();
    g.beginPath();
    g.moveTo(0, -bh * 0.28);
    g.lineTo(0, bh * 0.28);
    g.strokeStyle = rgba(0.5 + 0.4 * breath);
    g.lineWidth = 2.2;
    g.stroke();
    g.restore();
  }

  g.save();
  g.translate(cx, cy);
  g.rotate(rotTriangle);
  const rt = R * 0.34;
  g.beginPath();
  for (let i = 0; i < 3; i += 1) {
    const a = -Math.PI / 2 + i * ((Math.PI * 2) / 3);
    const x = Math.cos(a) * rt;
    const y = Math.sin(a) * rt;
    if (i) g.lineTo(x, y);
    else g.moveTo(x, y);
  }
  g.closePath();
  g.strokeStyle = rgba(0.55);
  g.lineWidth = 2;
  g.shadowColor = rgba(0.7);
  g.shadowBlur = 12;
  g.stroke();
  g.restore();
  g.shadowBlur = 0;

  const coreR = R * (0.16 + level * 0.17 + breath * 0.015);
  const radius = coreR * 2.4;
  const core = g.createRadialGradient(cx, cy, 0, cx, cy, radius);
  core.addColorStop(0, 'rgba(255,255,255,0.95)');
  core.addColorStop(0.22, rgba(0.95));
  core.addColorStop(0.55, rgba(0.32));
  core.addColorStop(1, rgba(0));
  g.fillStyle = core;
  g.beginPath();
  g.arc(cx, cy, radius, 0, Math.PI * 2);
  g.fill();

  g.beginPath();
  g.arc(cx, cy, coreR * 0.34, 0, Math.PI * 2);
  g.fillStyle = 'rgba(255,255,255,0.9)';
  g.fill();
}

function readVisual(): Visual {
  try {
    return localStorage.getItem(VISUAL_KEY) === 'logo' ? 'logo' : 'reactor';
  } catch {
    return 'reactor';
  }
}

/**
 * Spoken language. Whisper guesses it when left on "auto", and on a short
 * sentence it guesses wrong often enough to answer in Russian or Japanese.
 */
type SpeechLanguage = 'fr' | 'en' | 'auto';
const LANGUAGE_KEY = 'openjarvis-voice-language';

function readLanguage(): SpeechLanguage {
  try {
    const saved = localStorage.getItem(LANGUAGE_KEY);
    return saved === 'en' || saved === 'auto' ? saved : 'fr';
  } catch {
    return 'fr';
  }
}

/** Send one recording to the local Whisper server, pinning the language. */
async function transcribe(audio: Blob, language: SpeechLanguage): Promise<string> {
  const form = new FormData();
  form.append('file', audio, 'recording.webm');
  if (language !== 'auto') form.append('language', language);
  const res = await apiFetch('/v1/speech/transcribe', { method: 'POST', body: form });
  if (!res.ok) throw new Error(`La transcription a échoué (${res.status}).`);
  const data = await res.json();
  return String(data?.text ?? '').trim();
}

/** Wake-word relay served by hey_jarvis.py on this machine (Server-Sent Events). */
const WAKE_URL = 'http://127.0.0.1:8771/events';
const WAKE_RETRY_MS = 10000;

const MAX_HISTORY = 20;
const MAX_SPOKEN_CHARS = 4800; // the server refuses more than 5000
const BARS = 96;

function rmsOf(buffer: Uint8Array): number {
  let sum = 0;
  for (let i = 0; i < buffer.length; i += 1) {
    const v = (buffer[i] - 128) / 128;
    sum += v * v;
  }
  return Math.sqrt(sum / buffer.length);
}

export function VoicePage() {
  const selectedModel = useAppStore((s) => s.selectedModel);
  const serverModel = useAppStore((s) => s.serverInfo?.model ?? '');
  const model = selectedModel || serverModel;

  const [phase, setPhaseState] = useState<Phase>('idle');
  // "follow" by default: a short window for a follow-up question, then back to
  // waiting for the wake word. Unlimited re-listening ("continuous") makes
  // Jarvis reply to a TV or to other people in the room.
  const [afterReply, setAfterReply] = useState<AfterReply>(readAfterReply);
  const afterReplyRef = useRef<AfterReply>(afterReply);
  const followUpsRef = useRef(0); // follow-ups chained since the last wake/click
  const [followUp, setFollowUp] = useState(false); // current listen is a follow-up
  const [persona, setPersona] = useState<Persona>(readPersona);
  const personaRef = useRef<Persona>(persona);

  useEffect(() => {
    afterReplyRef.current = afterReply;
    try {
      localStorage.setItem(AFTER_KEY, afterReply);
    } catch {
      // Private browsing: the choice simply does not persist.
    }
  }, [afterReply]);

  useEffect(() => {
    personaRef.current = persona;
    try {
      localStorage.setItem(PERSONA_KEY, persona);
    } catch {
      // Private browsing: the choice simply does not persist.
    }
  }, [persona]);

  const [language, setLanguage] = useState<SpeechLanguage>(readLanguage);
  const languageRef = useRef<SpeechLanguage>(language);

  useEffect(() => {
    languageRef.current = language;
    try {
      localStorage.setItem(LANGUAGE_KEY, language);
    } catch {
      // Private browsing: the choice simply does not persist.
    }
  }, [language]);
  const [heard, setHeard] = useState('');
  const [reply, setReply] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [visual, setVisual] = useState<Visual>(readVisual);
  const visualRef = useRef<Visual>(visual);

  useEffect(() => {
    visualRef.current = visual;
    try {
      localStorage.setItem(VISUAL_KEY, visual);
    } catch {
      // Private browsing: the choice simply does not persist.
    }
  }, [visual]);

  // Everything the animation loop and the async turn pipeline read lives in
  // refs: they must see the current value without waiting for a re-render.
  const phaseRef = useRef<Phase>('idle');
  const modelRef = useRef(model);
  const runRef = useRef(0); // bumped on every stop; stale async steps check it
  const historyRef = useRef<Turn[]>([]);

  const stageRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  const audioCtxRef = useRef<AudioContext | null>(null);
  const micStreamRef = useRef<MediaStream | null>(null);
  const micSourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const micAnalyserRef = useRef<AnalyserNode | null>(null);
  const outAnalyserRef = useRef<AnalyserNode | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const vadRef = useRef<VoiceActivityDetector | null>(null);
  const playerRef = useRef<HTMLAudioElement | null>(null);
  const playerSourceRef = useRef<MediaElementAudioSourceNode | null>(null);
  const playerUrlRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const finishListeningRef = useRef<(send: boolean) => void>(() => {});

  useEffect(() => { modelRef.current = model; }, [model]);

  /**
   * The app loads its model list once at startup. If the server was still
   * starting at that moment the list stays empty for the whole session, so the
   * voice page asks the server again instead of giving up.
   */
  const resolveModel = useCallback(async (): Promise<string> => {
    if (modelRef.current) return modelRef.current;
    const store = useAppStore.getState();
    try {
      const info = await fetchServerInfo();
      store.setServerInfo(info);
      if (info.model) {
        modelRef.current = info.model;
        return info.model;
      }
    } catch {
      // fall through to the model list
    }
    try {
      const models = await fetchModels();
      store.setModels(models);
      const first = useAppStore.getState().selectedModel || models[0]?.id || '';
      modelRef.current = first;
      return first;
    } catch {
      return '';
    }
  }, []);

  // Recover as soon as the page opens, not only when the first question fails.
  useEffect(() => {
    if (!model) void resolveModel();
  }, [model, resolveModel]);

  const setPhase = useCallback((next: Phase) => {
    phaseRef.current = next;
    setPhaseState(next);
  }, []);

  // --- audio plumbing -------------------------------------------------------

  const ensureAudio = useCallback(async (): Promise<AudioContext> => {
    let ctx = audioCtxRef.current;
    if (!ctx) {
      ctx = new AudioContext();
      audioCtxRef.current = ctx;
      const out = ctx.createAnalyser();
      out.fftSize = 512;
      out.smoothingTimeConstant = 0.75;
      out.connect(ctx.destination);
      outAnalyserRef.current = out;
    }
    if (ctx.state === 'suspended') await ctx.resume();
    return ctx;
  }, []);

  const releaseMic = useCallback(() => {
    micSourceRef.current?.disconnect();
    micSourceRef.current = null;
    micAnalyserRef.current = null;
    micStreamRef.current?.getTracks().forEach((track) => track.stop());
    micStreamRef.current = null;
  }, []);

  const releasePlayer = useCallback(() => {
    const el = playerRef.current;
    if (el) {
      el.onended = null;
      el.onerror = null;
      el.pause();
      playerRef.current = null;
    }
    playerSourceRef.current?.disconnect();
    playerSourceRef.current = null;
    if (playerUrlRef.current) {
      URL.revokeObjectURL(playerUrlRef.current);
      playerUrlRef.current = null;
    }
  }, []);

  /** Stop whatever is going on and return to rest. */
  const stopAll = useCallback(() => {
    runRef.current += 1;
    abortRef.current?.abort();
    abortRef.current = null;
    const recorder = recorderRef.current;
    recorderRef.current = null;
    if (recorder && recorder.state !== 'inactive') {
      recorder.onstop = null;
      recorder.stop();
    }
    chunksRef.current = [];
    vadRef.current = null;
    releasePlayer();
    releaseMic();
    setFollowUp(false);
    setPhase('idle');
  }, [releaseMic, releasePlayer, setPhase]);

  const fail = useCallback((message: string) => {
    stopAll();
    setError(message);
  }, [stopAll]);

  // --- one conversational turn ---------------------------------------------

  /**
   * Open the microphone for one utterance. A follow-up (Jarvis re-listening on
   * its own after an answer) gives up sooner than a turn the user asked for.
   */
  const startListening = useCallback(async (opts: { followUpMs?: number } = {}) => {
    const run = runRef.current;
    const isFollowUp = opts.followUpMs !== undefined;
    if (!isFollowUp) followUpsRef.current = 0; // wake word / click: fresh exchange
    setFollowUp(isFollowUp);
    setError(null);

    if (!navigator.mediaDevices?.getUserMedia) {
      fail("Ce navigateur ne donne pas accès au microphone.");
      return;
    }
    try {
      const ctx = await ensureAudio();
      if (!micStreamRef.current) {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true },
        });
        if (run !== runRef.current) {
          stream.getTracks().forEach((track) => track.stop());
          return;
        }
        micStreamRef.current = stream;
        const source = ctx.createMediaStreamSource(stream);
        const analyser = ctx.createAnalyser();
        analyser.fftSize = 512;
        analyser.smoothingTimeConstant = 0.6;
        source.connect(analyser); // analysis only: never routed to the speakers
        micSourceRef.current = source;
        micAnalyserRef.current = analyser;
      }
      if (run !== runRef.current) return;

      const recorder = new MediaRecorder(micStreamRef.current);
      chunksRef.current = [];
      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.start();
      recorderRef.current = recorder;
      vadRef.current = new VoiceActivityDetector(
        isFollowUp ? { noSpeechTimeoutMs: opts.followUpMs } : {},
      );
      setPhase('listening');
    } catch {
      fail("Accès au microphone refusé. Autorisez-le dans le navigateur, puis réessayez.");
    }
  }, [ensureAudio, fail, setPhase]);

  const speak = useCallback(async (text: string, run: number) => {
    const spoken = toSpeakableText(text).slice(0, MAX_SPOKEN_CHARS);
    const finish = () => {
      if (run !== runRef.current) return;
      releasePlayer();
      const mode = afterReplyRef.current;
      if (mode === 'continuous') {
        void startListening({ followUpMs: 8000 });
      } else if (mode === 'follow' && followUpsRef.current < MAX_FOLLOW_UPS) {
        followUpsRef.current += 1;
        void startListening({ followUpMs: FOLLOW_UP_MS });
      } else {
        stopAll();
      }
    };
    if (!spoken) {
      finish();
      return;
    }

    setPhase('speaking');
    const ac = new AbortController();
    abortRef.current = ac;
    const blob = await synthesizeSpeech(spoken, { signal: ac.signal });
    if (run !== runRef.current) return;

    const ctx = await ensureAudio();
    const url = URL.createObjectURL(blob);
    const el = new Audio(url);
    playerUrlRef.current = url;
    playerRef.current = el;
    const source = ctx.createMediaElementSource(el);
    if (outAnalyserRef.current) source.connect(outAnalyserRef.current);
    playerSourceRef.current = source;
    el.onended = finish;
    el.onerror = () => {
      if (run === runRef.current) fail('La lecture audio a échoué.');
    };
    await el.play();
  }, [ensureAudio, fail, releasePlayer, setPhase, startListening, stopAll]);

  const processUtterance = useCallback(async (audio: Blob, run: number) => {
    setPhase('thinking');
    try {
      const transcript = await transcribe(audio, languageRef.current);
      if (run !== runRef.current) return;
      if (!transcript) {
        stopAll();
        setError("Je n'ai rien entendu. Réessayez en parlant plus près du micro.");
        return;
      }
      if (isWhisperHallucination(transcript)) {
        // Whisper made a sentence out of silence or noise: nobody asked anything.
        stopAll();
        return;
      }
      setHeard(transcript);
      setReply('');

      const chatModel = await resolveModel();
      if (run !== runRef.current) return;
      if (!chatModel) {
        fail("Aucun modèle disponible : le serveur OpenJarvis ou Ollama ne répond pas.");
        return;
      }

      const history = [...historyRef.current, { role: 'user' as const, content: transcript }];
      const ac = new AbortController();
      abortRef.current = ac;
      const res = await apiFetch('/v1/chat/completions', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          model: chatModel,
          messages: [
            { role: 'system', content: `${PERSONAS[personaRef.current].prompt} ${VOICE_RULES}` },
            ...history,
          ],
          stream: false,
        }),
        signal: ac.signal,
      });
      if (!res.ok) throw new Error(`Le modèle a répondu par une erreur (${res.status}).`);
      const data = await res.json();
      if (run !== runRef.current) return;

      const answer = stripThinkTags(String(data?.choices?.[0]?.message?.content ?? ''));
      if (!answer) throw new Error("Le modèle n'a rien répondu.");
      historyRef.current = [...history, { role: 'assistant' as const, content: answer }].slice(
        -MAX_HISTORY,
      );
      setReply(answer);
      await speak(answer, run);
    } catch (err) {
      if (run !== runRef.current) return;
      if (err instanceof DOMException && err.name === 'AbortError') return;
      fail(err instanceof Error ? err.message : 'Une erreur est survenue.');
    }
  }, [fail, resolveModel, setPhase, speak, stopAll]);

  /** End the recording; `send` false means the user said nothing. */
  const finishListening = useCallback((send: boolean) => {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state === 'inactive') return;
    const run = runRef.current;
    recorderRef.current = null;
    vadRef.current = null;

    if (!send) {
      stopAll();
      return;
    }
    recorder.onstop = () => {
      if (run !== runRef.current) return;
      const blob = new Blob(chunksRef.current, { type: recorder.mimeType || 'audio/webm' });
      chunksRef.current = [];
      void processUtterance(blob, run);
    };
    setPhase('thinking');
    recorder.stop();
  }, [processUtterance, setPhase, stopAll]);

  useEffect(() => { finishListeningRef.current = finishListening; }, [finishListening]);

  /** Barge-in: cut the answer short and listen to the user again. */
  const interruptAndListen = useCallback(() => {
    runRef.current += 1;
    abortRef.current?.abort();
    releasePlayer();
    void startListening();
  }, [releasePlayer, startListening]);

  /** Logo click / Space: start, send early, or interrupt. */
  const toggle = useCallback(() => {
    const current = phaseRef.current;
    if (current === 'idle') {
      void startListening();
    } else if (current === 'listening') {
      // Send what was said so far; if nothing was said, just stop.
      finishListening(vadRef.current?.speaking ?? false);
    } else if (current === 'speaking') {
      interruptAndListen();
    } else {
      stopAll();
    }
  }, [finishListening, interruptAndListen, startListening, stopAll]);

  // --- "Hey Jarvis" wake word -------------------------------------------------

  // Browsers only let a page start audio after the user has interacted with it
  // once. Until then a wake word could open the microphone but never answer
  // aloud, so the page asks for one click first.
  const [wakeOnline, setWakeOnline] = useState(false);
  const [armed, setArmed] = useState(
    () => typeof navigator !== 'undefined' && !!navigator.userActivation?.hasBeenActive,
  );

  useEffect(() => {
    if (armed) return;
    const arm = () => setArmed(true);
    window.addEventListener('pointerdown', arm, { once: true });
    window.addEventListener('keydown', arm, { once: true });
    return () => {
      window.removeEventListener('pointerdown', arm);
      window.removeEventListener('keydown', arm);
    };
  }, [armed]);

  useEffect(() => {
    let source: EventSource | null = null;
    let retry: ReturnType<typeof setTimeout> | undefined;
    let closed = false;

    const connect = () => {
      if (closed) return;
      source = new EventSource(WAKE_URL);
      source.onopen = () => setWakeOnline(true);
      source.addEventListener('wake', () => {
        const current = phaseRef.current;
        if (current === 'speaking') {
          // Saying "Hey Jarvis" over the answer cuts it short.
          interruptAndListen();
          return;
        }
        if (current !== 'idle') return; // already listening or thinking
        if (navigator.userActivation && !navigator.userActivation.hasBeenActive) {
          setError('Cliquez une fois sur la page pour autoriser le son, puis redites « Hey Jarvis ».');
          return;
        }
        void startListening();
      });
      source.onerror = () => {
        // The listener script is not running (or was stopped): retry quietly
        // instead of letting EventSource hammer the port every few seconds.
        setWakeOnline(false);
        source?.close();
        source = null;
        if (!closed) retry = setTimeout(connect, WAKE_RETRY_MS);
      };
    };

    connect();
    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      source?.close();
    };
  }, [interruptAndListen, startListening]);

  // --- keyboard -------------------------------------------------------------

  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target?.closest('input, textarea, select, button, [contenteditable="true"]')) return;
      if (e.code === 'Space' && !e.repeat) {
        e.preventDefault();
        toggle();
      } else if (e.key === 'Escape' && phaseRef.current !== 'idle') {
        e.preventDefault();
        stopAll();
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [stopAll, toggle]);

  // --- animation loop -------------------------------------------------------

  useEffect(() => {
    let raf = 0;
    let level = 0;
    const time = new Uint8Array(512);
    const freq = new Uint8Array(256);
    const bars = new Float32Array(BARS);
    const color = { ...REACTOR_COLORS.idle }; // displayed colour, eased towards the state's

    const frame = (now: number) => {
      raf = requestAnimationFrame(frame);
      const current = phaseRef.current;
      const analyser =
        current === 'listening'
          ? micAnalyserRef.current
          : current === 'speaking'
            ? outAnalyserRef.current
            : null;

      let target: number;
      if (analyser) {
        analyser.getByteTimeDomainData(time);
        analyser.getByteFrequencyData(freq);
        const rms = rmsOf(time);
        target = Math.min(1, rms * (current === 'listening' ? 6 : 3.2));

        if (current === 'listening' && vadRef.current) {
          const verdict = vadRef.current.step(rms, now);
          if (verdict === 'send') finishListeningRef.current(true);
          else if (verdict === 'timeout') finishListeningRef.current(false);
        }
      } else if (current === 'thinking') {
        freq.fill(0);
        target = 0.22 + 0.1 * Math.sin(now / 260);
      } else {
        freq.fill(0);
        target = 0.06 + 0.05 * Math.sin(now / 1100); // resting breath
      }
      // Fast attack, slow release: the logo jumps with the voice and settles gently.
      level += (target - level) * (target > level ? 0.45 : 0.1);

      const stage = stageRef.current;
      if (stage) stage.style.setProperty('--voice-level', level.toFixed(3));

      const canvas = canvasRef.current;
      const g = canvas?.getContext('2d');
      if (!canvas || !g) return;
      const dpr = window.devicePixelRatio || 1;
      const size = canvas.clientWidth;
      if (canvas.width !== Math.round(size * dpr)) {
        canvas.width = Math.round(size * dpr);
        canvas.height = Math.round(size * dpr);
      }
      g.setTransform(dpr, 0, 0, dpr, 0, 0);
      g.clearRect(0, 0, size, size);

      if (visualRef.current === 'reactor') {
        const want = REACTOR_COLORS[current];
        color.r += (want.r - color.r) * 0.05;
        color.g += (want.g - color.g) * 0.05;
        color.b += (want.b - color.b) * 0.05;
        drawReactor(g, size, now, level, color, current === 'thinking');
        return;
      }

      const c = size / 2;
      const inner = size * 0.33;
      const reach = size * 0.15;

      // Radial spectrum, mirrored so the ring is symmetric left/right.
      g.lineCap = 'round';
      g.lineWidth = Math.max(2, size / 150);
      for (let i = 0; i < BARS; i += 1) {
        const half = i < BARS / 2 ? i : BARS - 1 - i;
        const bin = 2 + Math.floor((half / (BARS / 2)) * 90);
        const want = analyser ? freq[bin] / 255 : 0;
        bars[i] += (want - bars[i]) * 0.35;
        const idle = current === 'thinking'
          ? 0.1 + 0.1 * Math.max(0, Math.sin(now / 300 - (i / BARS) * Math.PI * 2))
          : 0.035;
        const amp = Math.max(bars[i], idle);
        const angle = (i / BARS) * Math.PI * 2 - Math.PI / 2;
        const r1 = inner + reach * amp;
        g.strokeStyle = `rgba(${Math.round(56 + 120 * amp)}, ${Math.round(189 + 50 * amp)}, 255, ${0.25 + 0.75 * amp})`;
        g.beginPath();
        g.moveTo(c + Math.cos(angle) * inner, c + Math.sin(angle) * inner);
        g.lineTo(c + Math.cos(angle) * r1, c + Math.sin(angle) * r1);
        g.stroke();
      }

      // Thinking: a bright arc sweeping around the logo.
      if (current === 'thinking') {
        const start = (now / 420) % (Math.PI * 2);
        g.strokeStyle = 'rgba(125, 211, 252, 0.95)';
        g.lineWidth = Math.max(3, size / 110);
        g.beginPath();
        g.arc(c, c, inner - size * 0.022, start, start + Math.PI * 0.55);
        g.stroke();
      }
    };

    raf = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(raf);
  }, []);

  // Leaving the page must free the microphone and silence the voice.
  useEffect(() => () => {
    stopAll();
    void audioCtxRef.current?.close();
    audioCtxRef.current = null;
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <div className="voice-page flex-1 flex flex-col items-center justify-center min-h-0 px-6 py-8 overflow-y-auto">
      <style>{`
        .voice-stage {
          --voice-level: 0.06;
          position: relative;
          width: min(58vh, 78vw, 460px);
          aspect-ratio: 1;
          cursor: pointer;
          outline: none;
          -webkit-tap-highlight-color: transparent;
        }
        .voice-stage canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
        .voice-halo {
          position: absolute; inset: 14%;
          border-radius: 50%;
          background: radial-gradient(circle, rgba(56,189,248,0.55) 0%, rgba(37,99,235,0.22) 45%, transparent 70%);
          filter: blur(22px);
          opacity: calc(0.35 + var(--voice-level) * 0.9);
          transform: scale(calc(1 + var(--voice-level) * 0.35));
        }
        .voice-orbit {
          position: absolute; inset: 2%;
          border-radius: 50%;
          border: 1px dashed rgba(125, 211, 252, 0.28);
          animation: voice-spin 38s linear infinite;
        }
        .voice-orbit.inner {
          inset: 9%;
          border: 1px solid rgba(125, 211, 252, 0.14);
          border-top-color: rgba(125, 211, 252, 0.6);
          animation: voice-spin 9s linear infinite reverse;
        }
        .voice-stage[data-phase="thinking"] .voice-orbit.inner { animation-duration: 2.2s; }
        .voice-stage[data-phase="idle"] .voice-orbit { opacity: 0.55; }
        .voice-logo {
          position: absolute; inset: 20%;
          border-radius: 50%;
          overflow: hidden;
          background: #050b16;
          transform: scale(calc(1 + var(--voice-level) * 0.09));
          box-shadow:
            0 0 calc(18px + var(--voice-level) * 70px) rgba(56, 189, 248, calc(0.35 + var(--voice-level) * 0.55)),
            inset 0 0 22px rgba(2, 6, 23, 0.7);
          filter: brightness(calc(0.82 + var(--voice-level) * 0.5)) saturate(calc(1 + var(--voice-level) * 0.4));
        }
        .voice-stage[data-phase="idle"] .voice-logo { filter: brightness(0.78) saturate(0.9); }
        .voice-logo img {
          width: 100%; height: 100%;
          object-fit: cover;
          transform: scale(1.06); /* hide the picture's light corners behind the round mask */
          user-select: none; pointer-events: none;
        }
        .voice-stage:focus-visible .voice-logo { outline: 2px solid var(--color-accent); outline-offset: 6px; }
        @keyframes voice-spin { to { transform: rotate(360deg); } }
        @media (prefers-reduced-motion: reduce) {
          .voice-orbit { animation: none; }
        }
      `}</style>

      <div
        ref={stageRef}
        className="voice-stage shrink-0"
        data-phase={phase}
        role="button"
        tabIndex={0}
        aria-label={STATUS[phase]}
        onClick={toggle}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            toggle();
          }
        }}
      >
        {visual === 'logo' && (
          <>
            <div className="voice-halo" aria-hidden="true" />
            <div className="voice-orbit" aria-hidden="true" />
            <div className="voice-orbit inner" aria-hidden="true" />
          </>
        )}
        <canvas ref={canvasRef} aria-hidden="true" />
        {visual === 'logo' && (
          <div className="voice-logo">
            <img src="/openjarvis-logo.png" alt="OpenJarvis" draggable={false} />
          </div>
        )}
      </div>

      <div className="mt-6 w-full max-w-xl text-center" aria-live="polite">
        <p
          className="text-sm font-medium tracking-wide"
          style={{
            color:
              phase === 'idle'
                ? 'var(--color-text-secondary)'
                : visual === 'reactor'
                  ? `rgb(${REACTOR_COLORS[phase].r}, ${REACTOR_COLORS[phase].g}, ${REACTOR_COLORS[phase].b})`
                  : 'var(--color-accent)',
          }}
        >
          {phase === 'listening' && followUp ? 'Autre chose ? Je vous écoute encore…' : STATUS[phase]}
        </p>

        {error && (
          <p className="mt-3 text-sm" style={{ color: 'var(--color-error)' }}>
            {error}
          </p>
        )}

        {heard && (
          <p className="mt-4 text-sm" style={{ color: 'var(--color-text-tertiary)' }}>
            « {heard} »
          </p>
        )}
        {reply && (
          <p className="mt-2 text-base leading-relaxed" style={{ color: 'var(--color-text)' }}>
            {reply}
          </p>
        )}
      </div>

      <div
        className="mt-6 flex flex-wrap items-center justify-center gap-x-5 gap-y-2 text-xs"
        style={{ color: 'var(--color-text-tertiary)' }}
      >
        <label className="flex items-center gap-2 select-none">
          Après une réponse
          <select
            value={afterReply}
            onChange={(e) => {
              const v = e.target.value;
              setAfterReply(v === 'wake' || v === 'continuous' ? v : 'follow');
            }}
            className="rounded px-1.5 py-0.5 text-xs cursor-pointer"
            style={{
              background: 'var(--color-bg-secondary)',
              color: 'var(--color-text-secondary)',
              border: '1px solid var(--color-border)',
            }}
          >
            <option value="follow">Écouter la suite quelques secondes</option>
            <option value="wake">Attendre « Hey Jarvis »</option>
            <option value="continuous">Conversation continue</option>
          </select>
        </label>
        <label className="flex items-center gap-2 select-none">
          Caractère
          <select
            value={persona}
            onChange={(e) => {
              const v = e.target.value;
              setPersona(v === 'neutre' || v === 'concis' ? v : 'jarvis');
            }}
            className="rounded px-1.5 py-0.5 text-xs cursor-pointer"
            style={{
              background: 'var(--color-bg-secondary)',
              color: 'var(--color-text-secondary)',
              border: '1px solid var(--color-border)',
            }}
          >
            {(Object.keys(PERSONAS) as Persona[]).map((key) => (
              <option key={key} value={key}>
                {PERSONAS[key].label}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-2 select-none">
          Langue
          <select
            value={language}
            onChange={(e) => {
              const v = e.target.value;
              setLanguage(v === 'en' || v === 'auto' ? v : 'fr');
            }}
            className="rounded px-1.5 py-0.5 text-xs cursor-pointer"
            style={{
              background: 'var(--color-bg-secondary)',
              color: 'var(--color-text-secondary)',
              border: '1px solid var(--color-border)',
            }}
          >
            <option value="fr">Français</option>
            <option value="en">English</option>
            <option value="auto">Auto</option>
          </select>
        </label>
        <label className="flex items-center gap-2 select-none">
          Visuel
          <select
            value={visual}
            onChange={(e) => setVisual(e.target.value === 'logo' ? 'logo' : 'reactor')}
            className="rounded px-1.5 py-0.5 text-xs cursor-pointer"
            style={{
              background: 'var(--color-bg-secondary)',
              color: 'var(--color-text-secondary)',
              border: '1px solid var(--color-border)',
            }}
          >
            <option value="reactor">Réacteur</option>
            <option value="logo">Logo</option>
          </select>
        </label>
        <span
          className="flex items-center gap-1.5"
          title={
            wakeOnline
              ? 'Le script hey_jarvis.py écoute le micro'
              : 'Lancez hey_jarvis.py pour activer le mot « Hey Jarvis »'
          }
        >
          <span
            aria-hidden="true"
            className="inline-block w-1.5 h-1.5 rounded-full"
            style={{
              background: !wakeOnline
                ? 'var(--color-text-tertiary)'
                : armed
                  ? '#22c55e'
                  : 'var(--color-accent-amber)',
            }}
          />
          {!wakeOnline
            ? '« Hey Jarvis » : arrêté'
            : armed
              ? '« Hey Jarvis » : actif'
              : '« Hey Jarvis » : cliquez une fois sur la page'}
        </span>
        <span>{model ? `Modèle : ${model}` : 'Aucun modèle sélectionné'}</span>
        <span>Échap pour arrêter</span>
      </div>
    </div>
  );
}
