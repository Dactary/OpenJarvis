import { describe, expect, it } from 'vitest';
import { normalizeTrace } from './TraceDebugger';

describe('normalizeTrace', () => {
  it('accepts the step shape the API returns today', () => {
    const trace = normalizeTrace({
      id: 'abc',
      query: 'Quelle heure est-il ?',
      created_at: '2026-11-23T20:00:00',
      steps: [
        { step_type: 'generate', duration_seconds: 0.74, input: { a: 1 }, output: {}, metadata: {} },
        { step_type: 'respond', duration_seconds: 0, input: {}, output: { text: 'ok' }, metadata: {} },
      ],
    });
    expect(trace.steps[0].duration_ms).toBeCloseTo(740);
    expect(trace.steps[0].data.input).toEqual({ a: 1 });
    expect(trace.steps[1].duration_ms).toBe(0);
  });

  it('keeps the older { duration_ms, data } shape', () => {
    const trace = normalizeTrace({
      id: 'old', query: 'q', created_at: '', steps: [{ step_type: 'route', duration_ms: 12, data: { model: 'm' } }],
    });
    expect(trace.steps[0]).toEqual({ step_type: 'route', duration_ms: 12, data: { model: 'm' } });
  });

  it('survives a trace with no steps or missing fields', () => {
    expect(normalizeTrace({ id: 'x' }).steps).toEqual([]);
    const trace = normalizeTrace({ id: 'y', steps: [{}] });
    expect(trace.steps[0]).toMatchObject({ step_type: 'step', duration_ms: 0 });
    expect(Object.keys(trace.steps[0].data)).toContain('input');
  });
});
