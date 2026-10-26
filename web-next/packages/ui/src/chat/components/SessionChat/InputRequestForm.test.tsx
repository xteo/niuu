import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { InputRequestForm } from './InputRequestForm';
import type { InputRequest } from '../../types';

const request: InputRequest = {
  requestId: 'q',
  questions: [
    {
      id: 'hosts',
      header: 'Hosts',
      prompt: 'Which hosts?',
      multiSelect: true,
      choices: ['Thor', 'Bro'],
      descriptions: ['Local', 'Remote'],
    },
  ],
};

describe('InputRequestForm', () => {
  it('preserves multiple selections and custom text through submission and rejection', () => {
    const onRespond = vi.fn();
    const { rerender } = render(<InputRequestForm request={request} onRespond={onRespond} />);
    fireEvent.click(screen.getByRole('button', { name: 'Thor' }));
    fireEvent.click(screen.getByRole('button', { name: 'Bro' }));
    fireEvent.change(screen.getByLabelText('Which hosts?'), { target: { value: ' Spark ' } });
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }));
    expect(onRespond).toHaveBeenCalledWith('q', [
      {
        question: 'Which hosts?',
        question_id: 'hosts',
        header: 'Hosts',
        answer: ['Thor', 'Bro', 'Spark'],
        option_indexes: [0, 1],
        free_text: 'Spark',
      },
    ]);
    rerender(<InputRequestForm request={{ ...request, submitting: true }} onRespond={onRespond} />);
    expect(screen.getByRole('button', { name: 'Waiting for confirmation…' })).toBeDisabled();
    expect(screen.getByLabelText('Which hosts?')).toHaveValue(' Spark ');
    rerender(
      <InputRequestForm request={{ ...request, error: 'Menu not ready' }} onRespond={onRespond} />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('Menu not ready');
    fireEvent.click(screen.getByRole('button', { name: 'Thor' }));
    fireEvent.click(screen.getByRole('button', { name: 'Submit' }));
    expect(onRespond.mock.lastCall?.[1][0]).toMatchObject({
      answer: ['Bro', 'Spark'],
      option_indexes: [1],
    });
  });

  it('cannot submit while disconnected or when native recovery is required', () => {
    const onRespond = vi.fn();
    const { rerender } = render(
      <InputRequestForm request={request} disabled onRespond={onRespond} />,
    );
    expect(screen.getByRole('button', { name: 'Thor' })).toBeDisabled();
    rerender(
      <InputRequestForm request={{ ...request, answerable: false }} onRespond={onRespond} />,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('recovery');
    expect(screen.getByRole('button', { name: 'Submit' })).toBeDisabled();
    expect(onRespond).not.toHaveBeenCalled();
  });
});
