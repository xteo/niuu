import { describe, it, expect, afterEach } from 'vitest';
import { render, screen, cleanup, fireEvent } from '@testing-library/react';
import { ThemeProvider, useTheme, type ThemeName } from './ThemeProvider';

function ThemeReader() {
  const { theme } = useTheme();
  return <span data-testid="theme">{theme}</span>;
}

describe('ThemeProvider', () => {
  afterEach(() => {
    cleanup();
    localStorage.removeItem('niuu.theme');
    delete document.documentElement.dataset.theme;
  });

  it('defaults to midnight blue and sets data-theme on documentElement', () => {
    render(
      <ThemeProvider>
        <ThemeReader />
      </ThemeProvider>,
    );
    expect(screen.getByTestId('theme').textContent).toBe('midnight-blue');
    expect(document.documentElement.dataset.theme).toBe('midnight-blue');
  });

  it('respects initial theme prop', () => {
    render(
      <ThemeProvider theme="amber">
        <ThemeReader />
      </ThemeProvider>,
    );
    expect(document.documentElement.dataset.theme).toBe('amber');
  });

  it.each<ThemeName>(['xteo', 'midnight-blue', 'deep-blue', 'dark-blue'])(
    'switches to %s and restores it after remounting',
    (next) => {
      function ChooseTheme() {
        const { setTheme } = useTheme();
        return <button onClick={() => setTheme(next)}>Choose</button>;
      }
      const view = render(
        <ThemeProvider>
          <ChooseTheme />
        </ThemeProvider>,
      );
      fireEvent.click(screen.getByRole('button', { name: 'Choose' }));
      expect(document.documentElement.dataset.theme).toBe(next);
      expect(localStorage.getItem('niuu.theme')).toBe(next);
      view.unmount();
      render(
        <ThemeProvider>
          <ThemeReader />
        </ThemeProvider>,
      );
      expect(screen.getByTestId('theme')).toHaveTextContent(next);
    },
  );

  it('throws when useTheme is used outside the provider', () => {
    const originalError = console.error;
    console.error = () => {};
    expect(() => render(<ThemeReader />)).toThrow(/ThemeProvider/);
    console.error = originalError;
  });
});

function ThemeSwitch() {
  const { theme, setTheme } = useTheme();
  return <button onClick={() => setTheme(theme === 'xteo' ? 'ice' : 'xteo')}>{theme}</button>;
}
it('remembers the selected theme and restores the original dark palette after reload', async () => {
  const { fireEvent } = await import('@testing-library/react');
  localStorage.removeItem('niuu.theme');
  const first = render(
    <ThemeProvider theme="xteo">
      <ThemeSwitch />
    </ThemeProvider>,
  );
  fireEvent.click(screen.getByRole('button', { name: 'xteo' }));
  expect(document.documentElement.dataset.theme).toBe('ice');
  expect(localStorage.getItem('niuu.theme')).toBe('ice');
  first.unmount();
  const second = render(
    <ThemeProvider theme="xteo">
      <ThemeReader />
    </ThemeProvider>,
  );
  expect(screen.getByTestId('theme')).toHaveTextContent('ice');
  second.unmount();
  localStorage.removeItem('niuu.theme');
});
