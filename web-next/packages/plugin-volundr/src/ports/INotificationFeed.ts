import type {
  InstanceSeqMap,
  NotificationDelivery,
  NotificationReadState,
  NotificationRule,
  NotificationRuleDraft,
  NotificationServerFilter,
  NotificationSinkOption,
  SessionNotification,
} from '../domain/notifications';

export interface NotificationFeedPage {
  /** Newest first. */
  items: SessionNotification[];
  /** Opaque cursor for the next (older) page; `null` at the end of the feed. */
  nextBefore: string | null;
  /** Unread count reported with the page, when the server includes it. */
  unreadCount: number | null;
}

export interface NotificationGapPage {
  /** Oldest first — rows committed after the given watermarks. */
  items: SessionNotification[];
  /** True when the page was full and more rows may follow. */
  hasMore: boolean;
}

export type NotificationStreamStatus = 'open' | 'error';

export interface NotificationFeedSubscriber {
  onNotification(notification: SessionNotification): void;
  /**
   * Stream connection transitions. `open` is reported on every (re)connect —
   * and immediately on subscribe when the shared stream is already open — so
   * subscribers gap-fill with `listSince` there.
   */
  onStatus?(status: NotificationStreamStatus): void;
}

/** Rules, sinks and read-state writes target one Forge node; local when omitted. */
export interface NotificationInstanceOptions {
  instanceId?: string | null;
}

/**
 * The Forge notifications feed across every visible Forge node (contract §4–§6).
 * Cursors are opaque; seqs only compare within one instance.
 */
export interface INotificationFeed {
  list(
    filter: NotificationServerFilter,
    cursor?: { before?: string | null; limit?: number },
  ): Promise<NotificationFeedPage>;
  /** Gap-fill: rows after per-instance watermarks, ascending. */
  listSince(
    filter: NotificationServerFilter,
    after: InstanceSeqMap,
    options?: { limit?: number },
  ): Promise<NotificationGapPage>;
  /** One session's notifications after a seq, ascending. */
  listForSession(
    sessionId: string,
    after?: number | null,
    options?: NotificationInstanceOptions & { limit?: number },
  ): Promise<SessionNotification[]>;
  getReadState(): Promise<NotificationReadState>;
  /**
   * Move read watermarks forward for the given instances. Rejects with
   * `NotificationReadStateConflictError` when another reader moved them first.
   */
  markRead(
    through: InstanceSeqMap,
    expected: NotificationReadState,
  ): Promise<NotificationReadState>;
  subscribe(subscriber: NotificationFeedSubscriber): () => void;

  listRules(options?: NotificationInstanceOptions): Promise<NotificationRule[]>;
  createRule(
    draft: NotificationRuleDraft,
    options?: NotificationInstanceOptions,
  ): Promise<NotificationRule>;
  updateRule(
    id: string,
    draft: NotificationRuleDraft,
    options?: NotificationInstanceOptions,
  ): Promise<NotificationRule>;
  deleteRule(id: string, options?: NotificationInstanceOptions): Promise<void>;
  sinks(options?: NotificationInstanceOptions): Promise<NotificationSinkOption[]>;
  deliveries(
    notificationId: string,
    options?: NotificationInstanceOptions,
  ): Promise<NotificationDelivery[]>;
}
