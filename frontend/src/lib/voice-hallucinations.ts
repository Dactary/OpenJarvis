/**
 * Sentences Whisper invents on silence or background noise.
 *
 * Fed nothing but room tone, Whisper tends to output the credits it saw most
 * during training ("Sous-titres réalisés par la communauté d'Amara.org",
 * "Merci d'avoir regardé"...). Such a "sentence" must never be answered as if
 * the user had said it.
 *
 * Adapted from core/hallucinations.py of sosoj92/jarvis-assistant-vocal
 * (https://github.com/sosoj92/jarvis-assistant-vocal), MIT License,
 * Copyright (c) 2026 sosoj92.
 */

/** Markers specific to hallucinations: the sentence is dropped wherever they appear. */
const MARKERS = [
  'amara.org',
  'sous-titres realises',
  'sous titres realises',
  'sous-titrage st',
  'societe radio-canada',
  "merci d'avoir regarde",
  "n'oubliez pas de vous abonner",
  'thanks for watching',
  'thank you for watching',
  'subtitles by',
];

/**
 * Generic patterns a real request may contain ("active les sous-titres"), so
 * they only disqualify a sentence that starts with them.
 */
const STARTS = [
  'sous-titres',
  'sous titres',
  'sous-titrage',
  'abonnez-vous',
  'abonnez vous',
  'a la prochaine video',
];

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

// Whole words only ("sous-titrage st' 501" yes, "sous-titrage stp" no); starts
// are searched after leading punctuation and spaces.
const MARKER_RE = new RegExp(`(?<![\\p{L}\\p{N}_])(?:${MARKERS.map(escape).join('|')})(?![\\p{L}\\p{N}_])`, 'u');
const START_RE = new RegExp(`^[^\\p{L}\\p{N}]*(?:${STARTS.map(escape).join('|')})(?![\\p{L}\\p{N}_])`, 'u');

/** Lower-case, accents removed: used for loose comparisons. */
export function foldAccents(text: string): string {
  return text.toLowerCase().normalize('NFKD').replace(/\p{M}/gu, '');
}

/** True when a transcript looks like a known Whisper hallucination. */
export function isWhisperHallucination(text: string | null | undefined): boolean {
  const flat = foldAccents(String(text ?? '')).replace(/’/g, "'");
  return MARKER_RE.test(flat) || START_RE.test(flat);
}
