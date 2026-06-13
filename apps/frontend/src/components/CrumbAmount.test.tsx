/**
 * Unit tests for CrumbAmount — crumbs are never bare numbers.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import CrumbAmount from './CrumbAmount';

describe('CrumbAmount', () => {
  it('renders the amount and the pixel crumb glyph', () => {
    render(<CrumbAmount n={42} />);
    expect(screen.getByText('42')).toBeInTheDocument();
    const glyph = screen.getByTestId('crumb-glyph');
    expect(glyph).toBeInTheDocument();
    expect(glyph.getAttribute('aria-hidden')).toBe('true');
    // Hand-pixeled: a handful of <rect>s on a 7px grid, crisp edges, gold.
    expect(glyph.querySelectorAll('rect').length).toBeGreaterThanOrEqual(3);
    expect(glyph.getAttribute('width')).toBe('7');
    expect(glyph.getAttribute('class')).toContain('text-gold-500');
  });

  it('renders an explicit sign when signed', () => {
    render(<CrumbAmount n={8} signed />);
    expect(screen.getByText('+8')).toBeInTheDocument();
  });

  it('renders a minus sign for negative signed amounts', () => {
    render(<CrumbAmount n={-5} signed />);
    expect(screen.getByText('−5')).toBeInTheDocument();
  });

  it('passes className through to the wrapper (tinting)', () => {
    render(<CrumbAmount n={3} className="text-gold-300" />);
    expect(screen.getByText('3').parentElement?.className).toContain('text-gold-300');
  });
});
