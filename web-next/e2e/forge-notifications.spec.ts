import { test, expect, type Page } from '@playwright/test';
import { readFileSync } from 'node:fs';

/**
 * Forge notifications feed against a faked Guild facade (contract §4–§6):
 * the web asks `{forge}/notifications?all_instances=true`, listens for
 * `session_notification` on the shared fleet stream and gap-fills with `after`
 * when that stream reconnects.
 */
const config = JSON.parse(
  readFileSync(new URL('../apps/niuu/public/config.json', import.meta.url), 'utf8'),
);
const origin = 'http://forge-notifications.invalid';
const FORGE = '/api/v1/forge';

interface Row {
  id: string;
  seq: number;
  instance_id: string;
  session_id: string | null;
  session_name: string | null;
  kind: string;
  severity: string;
  source: string;
  title: string;
  body: string;
  created_at: string;
  engine?: string | null;
  model?: string | null;
  project_id?: string | null;
  links?: unknown[];
}

function row(overrides: Partial<Row> & Pick<Row, 'id' | 'seq' | 'title'>, minutesAgo: number): Row {
  return {
    instance_id: 'thor',
    session_id: 'sess-api',
    session_name: 'forge-api',
    kind: 'info',
    severity: 'info',
    source: 'agent',
    body: '',
    engine: 'claude',
    model: 'claude-opus-5-5',
    project_id: null,
    links: [],
    created_at: new Date(Date.now() - minutesAgo * 60_000).toISOString(),
    ...overrides,
  };
}

function decodeCursor(cursor: string): unknown {
  return JSON.parse(Buffer.from(cursor, 'base64url').toString('utf8'));
}

interface FixtureOptions {
  /** Hold the first page until released. */
  holdList?: boolean;
}

async function fixture(page: Page, options: FixtureOptions = {}) {
  const rows: Row[] = [
    row(
      {
        id: 'n-attn',
        seq: 7,
        kind: 'attention',
        severity: 'warning',
        source: 'system',
        title: 'forge-api needs your input',
        body: 'Waiting on a permission prompt.',
      },
      2,
    ),
    row(
      {
        id: 'n-crash',
        seq: 4,
        instance_id: 'horde-1',
        session_id: 'sess-bench',
        session_name: 'gpu-bench',
        kind: 'error',
        severity: 'critical',
        title: 'Benchmark run crashed',
        body: 'CUDA OOM on node 3.',
        engine: 'codex',
        model: 'gpt-6-astra',
      },
      9,
    ),
    row(
      {
        id: 'n-mig',
        seq: 6,
        kind: 'milestone',
        severity: 'success',
        title: 'Migration 000069 applied',
        body: 'All **four** tables created.',
      },
      15,
    ),
  ];
  const readState = {
    thor: { read_through_seq: 5, revision: 1, head_seq: 7 },
    'horde-1': { read_through_seq: 0, revision: 0, head_seq: 4 },
  };
  const state = {
    listCalls: 0,
    /** While true, the feed answers 500 (the app retries once on its own). */
    failList: false,
    gapCursors: [] as string[],
    readStatePuts: [] as unknown[],
  };
  let releaseList!: () => void;
  const listGate = new Promise<void>((resolve) => (releaseList = resolve));
  // Open fleet-stream requests wait here until the test pushes a body; the dev
  // build mounts effects twice, so a push answers every waiting connection.
  let streamWaiters: Array<(body: string) => void> = [];
  function pushStream(body: string) {
    const waiting = streamWaiters;
    streamWaiters = [];
    for (const resolve of waiting) resolve(body);
  }
  const live = row(
    { id: 'n-live', seq: 8, kind: 'milestone', severity: 'success', title: 'Deploy finished' },
    0,
  );
  const gap = row({ id: 'n-gap', seq: 9, kind: 'decision', title: 'Chose the facade cursor' }, 0);

  function unread() {
    const counts = Object.fromEntries(
      Object.entries(readState).map(([id, s]) => [
        id,
        rows.filter((r) => r.instance_id === id && r.seq > s.read_through_seq).length,
      ]),
    );
    return {
      unread_count: Object.values(counts).reduce((a, b) => a + b, 0),
      instances: Object.fromEntries(
        Object.entries(readState).map(([id, s]) => [
          id,
          {
            ...s,
            unread_count: counts[id],
            head_seq: Math.max(0, ...rows.filter((r) => r.instance_id === id).map((r) => r.seq)),
          },
        ]),
      ),
    };
  }

  await page.route(/\/config(?:\.live)?\.json$/, (route) =>
    route.fulfill({
      json: {
        ...config,
        theme: 'xteo',
        services: {
          ...config.services,
          niuu: { mode: 'http', baseUrl: origin + '/api/v1/niuu' },
          forge: { mode: 'http', baseUrl: origin + FORGE },
          volundr: { mode: 'http', baseUrl: origin + '/api/v1/volundr' },
        },
      },
    }),
  );
  await page.route(origin + '/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    const method = route.request().method();
    if (path.endsWith('/instances'))
      return route.fulfill({
        json: [
          {
            id: 'thor',
            slug: 'thor',
            name: 'Thor',
            kind: 'volundr',
            enabled: true,
            baseUrl: origin,
          },
          {
            id: 'horde-1',
            slug: 'horde-1',
            name: 'Horde 1',
            kind: 'volundr',
            enabled: true,
            baseUrl: origin,
          },
        ],
      });
    if (path === `${FORGE}/sessions/stream`) {
      expect(url.searchParams.get('all_instances')).toBe('true');
      const body = await new Promise<string>((resolve) => streamWaiters.push(resolve));
      // The browser may have dropped this connection meanwhile.
      return route.fulfill({ contentType: 'text/event-stream', body }).catch(() => undefined);
    }
    if (path === `${FORGE}/notifications/read-state`) {
      expect(url.searchParams.get('all_instances')).toBe('true');
      if (method === 'PUT') {
        const body = route.request().postDataJSON() as {
          instances: Record<string, { read_through_seq: number; expected_revision: number }>;
        };
        state.readStatePuts.push(body);
        for (const [id, next] of Object.entries(body.instances)) {
          const current = readState[id as keyof typeof readState];
          if (next.expected_revision !== current.revision)
            return route.fulfill({ status: 409, json: { detail: 'revision conflict' } });
          current.read_through_seq = Math.max(current.read_through_seq, next.read_through_seq);
          current.revision += 1;
        }
      }
      return route.fulfill({ json: unread() });
    }
    if (path === `${FORGE}/notifications/sinks`)
      return route.fulfill({
        json: [
          { name: 'telegram', label: 'Telegram', requires_integration: true },
          { name: 'push', label: 'Push', requires_integration: false },
        ],
      });
    if (path === `${FORGE}/notifications/rules`) return route.fulfill({ json: [] });
    if (path === `${FORGE}/notifications/n-crash/deliveries`)
      return route.fulfill({
        json: [{ id: 'd1', rule_id: 'r1', sink: 'telegram', status: 'delivered', attempts: 1 }],
      });
    if (path === `${FORGE}/notifications`) {
      expect(url.searchParams.get('all_instances')).toBe('true');
      const after = url.searchParams.get('after');
      if (after) {
        state.gapCursors.push(after);
        const marks = decodeCursor(after) as Record<string, number>;
        const newer = rows
          .filter((r) => marks[r.instance_id] !== undefined && r.seq > marks[r.instance_id]!)
          .sort((a, b) => a.seq - b.seq);
        return route.fulfill({ json: { items: newer, next_before: null } });
      }
      state.listCalls += 1;
      if (options.holdList) await listGate;
      if (state.failList)
        return route.fulfill({ status: 500, json: { detail: 'notification store unavailable' } });
      const items = [...rows].sort((a, b) => b.created_at.localeCompare(a.created_at));
      return route.fulfill({
        json: { items, next_before: null, unread_count: unread().unread_count },
      });
    }
    return route.fulfill({ json: path.endsWith('/stats') ? {} : [] });
  });
  return {
    state,
    releaseList,
    waitingStreams: () => streamWaiters.length,
    /** Commit the live row and deliver it on the open fleet stream. */
    sendLive() {
      rows.push(live);
      pushStream(`event: session_notification\ndata: ${JSON.stringify(live)}\n\n`);
    },
    /** Commit a row nobody hears about, then accept the reconnect. */
    reconnectAfterGap() {
      rows.push(gap);
      pushStream(': reconnected\n\n');
    },
  };
}

const feedRows = (page: Page) => page.getByTestId('notification-row');

test('notifications feed: live rows, reconnect gap-fill, filters, delivery and mark read', async ({
  page,
}, info) => {
  const fake = await fixture(page);
  const { state } = fake;
  await page.goto('/volundr/notifications');

  await expect(page.getByRole('heading', { name: 'Notifications' })).toBeVisible();
  await expect(feedRows(page)).toHaveCount(3);
  await expect(feedRows(page).first()).toContainText('forge-api needs your input');
  await expect(page.getByTestId('tab-count-notifications')).toHaveText('3');
  await expect(page.getByTestId('notifications-unread')).toHaveText('3 unread');

  // A live session_notification lands on top and counts as unread.
  await expect.poll(fake.waitingStreams).toBeGreaterThan(0);
  fake.sendLive();
  await expect(feedRows(page).first()).toContainText('Deploy finished');
  await expect(page.getByTestId('tab-count-notifications')).toHaveText('4');

  // The stream drops; a row committed meanwhile arrives through gap-fill with
  // `after` = the per-instance watermarks once the stream reconnects.
  await expect.poll(fake.waitingStreams, { timeout: 10_000 }).toBeGreaterThan(0);
  fake.reconnectAfterGap();
  await expect(feedRows(page).first()).toContainText('Chose the facade cursor');
  expect(state.gapCursors.map(decodeCursor)).toContainEqual({ thor: 8, 'horde-1': 4 });
  await expect(page.getByTestId('tab-count-notifications')).toHaveText('5');
  await expect(page.getByRole('status', { name: 'Live', exact: true })).toBeVisible();

  // Filters are applied and survive a reload.
  const filters = page.getByRole('group', { name: 'Notification filters' });
  await filters.getByRole('switch', { name: 'Error' }).click();
  await expect(feedRows(page)).toHaveCount(1);
  await expect(feedRows(page).first()).toContainText('Benchmark run crashed');
  await page.reload();
  await expect(feedRows(page)).toHaveCount(1);
  await expect(page.getByRole('switch', { name: 'Error' })).toHaveAttribute('aria-checked', 'true');

  // The expanded row shows the body and where it was delivered.
  await feedRows(page).first().getByRole('button', { name: 'Show details' }).click();
  await expect(feedRows(page).first()).toContainText('CUDA OOM on node 3.');
  await expect(feedRows(page).first().getByTestId('notification-delivery')).toContainText(
    'Delivered',
  );
  await page.getByRole('button', { name: 'Clear all filters' }).click();
  await expect(feedRows(page)).toHaveCount(5);
  await page.screenshot({
    path: info.outputPath('notifications-feed.png'),
    animations: 'disabled',
  });

  // Mark all read moves every host's watermark forward in one facade write.
  await page.getByRole('button', { name: 'Mark all read' }).click();
  await expect(page.getByTestId('notifications-unread')).toHaveText('All read');
  await expect(page.getByTestId('tab-count-notifications')).toHaveCount(0);
  expect(state.readStatePuts.at(-1)).toEqual({
    instances: {
      thor: { read_through_seq: 9, expected_revision: 1 },
      'horde-1': { read_through_seq: 4, expected_revision: 0 },
    },
  });

  // Opening a row goes to its session, anchored at the notification.
  await feedRows(page)
    .filter({ hasText: 'Benchmark run crashed' })
    .getByRole('button', { name: /Benchmark run crashed — open gpu-bench/ })
    .click();
  await expect(page).toHaveURL(/\/volundr\/session\/sess-bench#notification-n-crash$/);
});

test('notifications feed shows a loading state before the first page', async ({ page }) => {
  const { releaseList } = await fixture(page, { holdList: true });
  await page.goto('/volundr/notifications');
  await expect(page.getByRole('status', { name: 'Loading notifications…' })).toBeVisible();
  releaseList();
  await expect(feedRows(page)).toHaveCount(3);
  await expect(page.getByRole('status', { name: 'Loading notifications…' })).toHaveCount(0);
});

test('notifications feed reports a failed load and recovers on retry', async ({ page }) => {
  const { state } = await fixture(page);
  state.failList = true;
  await page.goto('/volundr/notifications');
  const error = page.getByRole('alert').filter({ hasText: 'Could not load notifications' });
  await expect(error).toBeVisible({ timeout: 10_000 });
  await expect(error).toContainText('notification store unavailable');
  state.failList = false;
  await error.getByRole('button', { name: 'Retry' }).click();
  await expect(feedRows(page)).toHaveCount(3);
});

test('notifications feed is keyboard accessible', async ({ page }) => {
  await fixture(page);
  await page.goto('/volundr/notifications');
  await expect(feedRows(page)).toHaveCount(3);

  // The rules panel opens from the keyboard and Escape returns focus to its toggle.
  const toggle = page.getByRole('button', { name: 'Delivery rules', exact: true });
  await toggle.focus();
  await page.keyboard.press('Enter');
  const panel = page.getByRole('complementary', { name: 'Delivery rules' });
  await expect(panel).toContainText('No delivery rules');
  await panel.getByRole('button', { name: 'New rule' }).focus();
  await page.keyboard.press('Escape');
  await expect(panel).toHaveCount(0);
  await expect(toggle).toBeFocused();

  // j/k move between rows; Tab reaches a row's actions; Enter opens the session.
  const open = (index: number) => feedRows(page).nth(index).locator('[data-notification-open]');
  await open(0).focus();
  await page.keyboard.press('j');
  await expect(open(1)).toBeFocused();
  await page.keyboard.press('ArrowDown');
  await expect(open(2)).toBeFocused();
  await page.keyboard.press('k');
  await expect(open(1)).toBeFocused();
  await page.keyboard.press('Tab');
  await expect(
    feedRows(page).nth(1).getByRole('button', { name: 'Read through here' }),
  ).toBeFocused();
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('notifications-unread')).toHaveText('1 unread');
  await open(1).focus();
  await page.keyboard.press('Enter');
  await expect(page).toHaveURL(/\/volundr\/session\/sess-bench#notification-n-crash$/);
});

async function sessionFixture(page: Page) {
  const session = {
    id: 'review',
    name: 'Notify review',
    status: 'running',
    activity_state: 'idle',
    model: 'claude-opus-5-5',
    source: { type: 'local_mount', local_path: '/workspace' },
    chat_endpoint: `${origin.replace('http:', 'ws:')}/forge-host/thor/s/review/session`,
    created_at: '2026-09-23T09:00:00Z',
  };
  const draft = {
    kind: 'milestone',
    severity: 'success',
    title: 'Migration 000069 applied',
    body: 'All **four** notification tables exist.',
    links: [{ label: 'PR #412', url: 'https://github.com/niuulabs/niuu/pull/412', kind: 'pr' }],
  };
  await page.route(/\/config(?:\.live)?\.json$/, (route) =>
    route.fulfill({
      json: {
        ...config,
        theme: 'xteo',
        services: {
          ...config.services,
          forge: { mode: 'http', baseUrl: origin + FORGE },
          volundr: { mode: 'http', baseUrl: origin + '/api/v1/volundr' },
        },
      },
    }),
  );
  await page.routeWebSocket(`${origin.replace('http:', 'ws:')}/**`, () => {});
  await page.route(origin + '/**', async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (path.endsWith('/instances'))
      return route.fulfill({
        json: [{ id: 'thor', name: 'Thor', kind: 'volundr', enabled: true, baseUrl: origin }],
      });
    if (path.endsWith('/sessions/stream')) return new Promise(() => {});
    if (path.endsWith('/conversation'))
      return route.fulfill({
        json: {
          turns: [
            {
              id: 'nt_0123456789abcdef0123456789abcdef',
              role: 'assistant',
              content: draft.title,
              created_at: '2026-09-23T09:01:00Z',
              parts: [
                {
                  type: 'tool_use',
                  id: 'nt_0123456789abcdef0123456789abcdef',
                  name: 'forge_notification',
                  input: { ...draft, notification_id: 'n-1' },
                },
              ],
              metadata: {
                kind: 'notification',
                notification: { ...draft, notification_id: 'n-1' },
              },
              visibility: 'public',
            },
            {
              id: 'turn-1',
              role: 'assistant',
              content: 'The migration is in; I let you know.',
              created_at: '2026-09-23T09:01:05Z',
              parts: [
                { type: 'text', text: 'The migration is in; I let you know.' },
                { type: 'tool_use', id: 'call-1', name: 'mcp__forge__notify', input: draft },
                {
                  type: 'tool_result',
                  tool_use_id: 'call-1',
                  content: '{"notification_id":"n-1","state":"committed"}',
                },
              ],
              visibility: 'public',
            },
          ],
          total_turns: 2,
          window_offset: 0,
          window_end: 2,
          history_protocol: 2,
          older_cursor: null,
          projection_revision: 'notify',
        },
      });
    return route.fulfill({
      json: path.endsWith('/sessions/review')
        ? session
        : path.endsWith('/sessions')
          ? [session]
          : path.includes('/workflow/gates')
            ? { gates: [] }
            : path.includes('/features/modules')
              ? [{ key: 'chat', scope: 'session', enabled: true, label: 'Chat', order: 0 }]
              : path.includes('/stats') || path.endsWith('/read-state')
                ? {}
                : [],
    });
  });
}

test('a notification turn renders as a card in the session transcript and takes deep links', async ({
  page,
}, info) => {
  await sessionFixture(page);
  await page.goto('/volundr/session/review#notification-n-1');
  const card = page.getByTestId('notification-card');
  await expect(card).toBeVisible();
  await expect(card).toHaveAttribute('data-targeted', 'true');
  await expect(card).toContainText('Migration 000069 applied');
  await expect(card).toContainText('All four notification tables exist.');
  await expect(card.getByRole('link', { name: 'PR #412' })).toHaveAttribute('target', '_blank');
  // The raw MCP call is one collapsed, expandable "Notified" line — never a second card.
  await expect(page.getByTestId('notification-card')).toHaveCount(1);
  await expect(page.getByText('mcp:forge__notify')).toHaveCount(0);
  const raw = page.locator('[data-tool-kind="forge-notify"]');
  await expect(raw).toHaveCount(1);
  const disclosure = raw.getByRole('button', { expanded: false });
  await expect(disclosure).toContainText('Notified');
  await disclosure.click();
  await expect(raw).toContainText('"state":"committed"');
  await page.screenshot({ path: info.outputPath('notification-card.png'), animations: 'disabled' });
});
