import { describe, expect, it } from 'vitest';
import { describeToolRequest, parseConfirmation } from './voice-confirm';

describe('parseConfirmation', () => {
  it('recognises an approval', () => {
    for (const s of ['Oui.', 'Ouais, vas-y !', "D'accord", 'OK', 'Oui, exécute.', 'Bien sûr']) {
      expect(parseConfirmation(s), s).toBe('yes');
    }
  });

  it('recognises a refusal', () => {
    for (const s of ['Non.', 'Non merci', 'Annule', 'Surtout pas', 'Stop', 'Arrête']) {
      expect(parseConfirmation(s), s).toBe('no');
    }
  });

  it('lets a refusal win over an approval', () => {
    expect(parseConfirmation('Oui... euh non')).toBe('no');
    expect(parseConfirmation("Non, ne l'exécute pas")).toBe('no');
  });

  it('does not guess', () => {
    for (const s of ['', 'Peut-être', 'Quelle heure est-il ?', 'Nonobstant', 'Louis']) {
      expect(parseConfirmation(s), s).toBeNull();
    }
  });
});

describe('describeToolRequest', () => {
  it('reads a short shell command aloud', () => {
    const r = describeToolRequest({ tool: 'shell_exec', arguments: { command: 'dir' } });
    expect(r.detail).toBe('dir');
    expect(r.spoken).toContain('dir');
  });

  it('does not read a long command aloud', () => {
    const command = 'x'.repeat(200);
    const r = describeToolRequest({ tool: 'shell_exec', arguments: { command } });
    expect(r.detail).toBe(command);
    expect(r.spoken).not.toContain('xxx');
  });

  it('shows Python code', () => {
    const r = describeToolRequest({ tool: 'code_interpreter', arguments: { code: 'print(1)' } });
    expect(r.title).toBe('Code Python');
    expect(r.detail).toBe('print(1)');
  });

  it('copes with unknown tools and unparsed arguments', () => {
    expect(describeToolRequest({ tool: 'file_write', arguments: "{'a': 1}" }).detail).toBe("{'a': 1}");
    expect(describeToolRequest(null).title).toBe('Outil outil');
  });
});
