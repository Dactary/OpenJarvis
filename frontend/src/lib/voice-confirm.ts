/**
 * Spoken confirmation of a sensitive tool call ("shell_exec", "code_interpreter").
 *
 * The server queues the request (action_type "tool_confirmation") and waits;
 * the voice page reads the question aloud and turns the user's "oui" / "non"
 * into an approval or a refusal.
 */

import { foldAccents } from './voice-hallucinations';

export const TOOL_CONFIRMATION = 'tool_confirmation';

export interface ToolRequest {
  /** Short on-screen title. */
  title: string;
  /** What exactly would run (command, code, arguments). */
  detail: string;
  /** The question read aloud. */
  spoken: string;
}

const MAX_SPOKEN_COMMAND = 90;

function argText(args: unknown, key: string): string {
  if (args && typeof args === 'object' && key in args) {
    const value = (args as Record<string, unknown>)[key];
    if (typeof value === 'string') return value;
  }
  return '';
}

/** Turn a queued confirmation into what the page shows and says. */
export function describeToolRequest(payload: Record<string, unknown> | null | undefined): ToolRequest {
  const tool = typeof payload?.tool === 'string' ? payload.tool : 'outil';
  const args = payload?.arguments;
  const raw = typeof args === 'string' ? args : JSON.stringify(args ?? {}, null, 2);

  if (tool === 'shell_exec') {
    const command = argText(args, 'command') || raw;
    const short =
      command.length > MAX_SPOKEN_COMMAND
        ? 'la commande affichée à l’écran'
        : `la commande : ${command}`;
    return {
      title: 'Commande système',
      detail: command,
      spoken: `Dois-je exécuter ${short} ? Dites oui ou non.`,
    };
  }
  if (tool === 'code_interpreter') {
    return {
      title: 'Code Python',
      detail: argText(args, 'code') || raw,
      spoken: 'Dois-je exécuter le code Python affiché à l’écran ? Dites oui ou non.',
    };
  }
  return {
    title: `Outil ${tool}`,
    detail: raw,
    spoken: `Dois-je utiliser l’outil ${tool.replace(/_/g, ' ')} ? Dites oui ou non.`,
  };
}

// A refusal anywhere in the sentence wins: "oui... euh non" must not run anything.
const NO = ['non', 'no', 'nan', 'pas', 'jamais', 'annule', 'annuler', 'refuse', 'refuser', 'stop', 'arrete'];
const YES = [
  'oui', 'ouais', 'yes', 'ok', 'okay', 'accord', 'autorise', 'autoriser', 'confirme',
  'confirmer', 'execute', 'executer', 'lance', 'affirmatif', 'vas-y', 'vas y', 'fais-le',
  'fais le', 'bien sur', 'go',
];

function contains(words: string[], padded: string): boolean {
  return words.some((word) => padded.includes(` ${word} `));
}

/** 'yes', 'no', or null when the answer is not clearly either. */
export function parseConfirmation(text: string | null | undefined): 'yes' | 'no' | null {
  const flat = foldAccents(String(text ?? '')).replace(/[’']/g, ' ');
  const padded = ` ${flat.replace(/[^\p{L}\p{N}-]+/gu, ' ').trim()} `;
  if (contains(NO, padded)) return 'no';
  if (contains(YES, padded)) return 'yes';
  return null;
}
