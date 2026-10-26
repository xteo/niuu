import { useId, useState, type FormEvent } from 'react';
import type { InputAnswer, InputRequest } from '../../types';

export function InputRequestForm({
  request,
  disabled = false,
  onRespond,
}: {
  request: InputRequest;
  disabled?: boolean;
  onRespond: (requestId: string, values: (string | InputAnswer)[]) => void;
}) {
  const formId = useId();
  const [selected, setSelected] = useState<number[][]>(() => request.questions.map(() => []));
  const [text, setText] = useState(() => request.questions.map(() => ''));
  const answers: InputAnswer[] = request.questions.map((question, index) => {
    const indexes = selected[index] ?? [];
    const custom = (text[index] ?? '').trim();
    const values = indexes.map((i) => question.choices[i]!).concat(custom ? [custom] : []);
    return {
      question: question.prompt,
      ...(question.id ? { question_id: question.id } : {}),
      ...(question.header ? { header: question.header } : {}),
      answer: question.multiSelect ? values : (values[0] ?? ''),
      option_indexes: indexes,
      ...(custom ? { free_text: custom } : {}),
    };
  });
  const unavailable = disabled || request.submitting || request.answerable === false;
  const complete = answers.every((answer) => answer.answer.length > 0);
  const submit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (unavailable || !complete) return;
    onRespond(request.requestId, answers);
  };

  return (
    <section className="niuu-chat-request">
      <form className="niuu-chat-request-copy" onSubmit={submit}>
        <strong className="niuu-chat-request-title">Input required</strong>
        {request.questions.map((question, index) => (
          <fieldset
            className="niuu-chat-request-question"
            key={`${request.requestId}-${index}`}
            disabled={unavailable}
          >
            <legend className="niuu-chat-request-prompt">{question.prompt}</legend>
            {question.multiSelect && <small>Select all that apply.</small>}
            {question.choices.length > 0 && (
              <div className="niuu-chat-request-choices" aria-label="Suggested responses">
                {question.choices.map((choice, choiceIndex) => (
                  <button
                    type="button"
                    className="niuu-chat-request-button niuu-chat-request-choice"
                    aria-label={choice}
                    aria-describedby={
                      question.descriptions?.[choiceIndex]
                        ? `${formId}-${index}-${choiceIndex}`
                        : undefined
                    }
                    aria-pressed={(selected[index] ?? []).includes(choiceIndex)}
                    key={choiceIndex}
                    onClick={() => {
                      setSelected((current) =>
                        current.map((value, i) =>
                          i !== index
                            ? value
                            : question.multiSelect
                              ? value.includes(choiceIndex)
                                ? value.filter((v) => v !== choiceIndex)
                                : [...value, choiceIndex]
                              : [choiceIndex],
                        ),
                      );
                      if (!question.multiSelect) {
                        setText((current) => current.map((value, i) => (i === index ? '' : value)));
                      }
                    }}
                  >
                    <span>{choice}</span>
                    {question.descriptions?.[choiceIndex] && (
                      <small id={`${formId}-${index}-${choiceIndex}`}>
                        {question.descriptions[choiceIndex]}
                      </small>
                    )}
                  </button>
                ))}
              </div>
            )}
            <input
              className="niuu-chat-request-input"
              aria-label={question.prompt}
              value={text[index] ?? ''}
              onChange={(event) => {
                setText((current) =>
                  current.map((value, i) => (i === index ? event.target.value : value)),
                );
                if (!question.multiSelect) {
                  setSelected((current) => current.map((value, i) => (i === index ? [] : value)));
                }
              }}
              placeholder={question.choices.length ? 'Or type another answer' : 'Type a response'}
            />
          </fieldset>
        ))}
        {request.error && (
          <p role="alert" className="niuu-chat-request-error">
            {request.error}
          </p>
        )}
        {request.answerable === false && !request.error && (
          <p role="alert">This question needs recovery in the session terminal.</p>
        )}
        <button
          type="submit"
          className="niuu-chat-request-button niuu-chat-request-button--primary"
          disabled={unavailable || !complete}
        >
          {request.submitting ? 'Waiting for confirmation…' : 'Submit'}
        </button>
      </form>
    </section>
  );
}
