import assert from "node:assert/strict";
import { test } from "node:test";

import { SessionStreamAggregator } from "./session-stream-aggregator";
import type { SessionRuntimeEvent, SessionThread, StoredSessionEvent } from "../types/sessions";

function runtimeEvent(partial: Partial<SessionRuntimeEvent> & { event: string }): SessionRuntimeEvent {
  return {
    session_key: "s",
    ...partial,
  };
}

function stored(partial: Partial<StoredSessionEvent> & { event_type: string; seq_no: number }): StoredSessionEvent {
  return {
    id: `e${partial.seq_no}`,
    content: "",
    created_at: "2026-09-15T00:00:00Z",
    metadata: {},
    ...partial,
  };
}

function runningThread(events: StoredSessionEvent[]): SessionThread {
  return {
    key: "s",
    title: "调研对话",
    status: "running",
    messages: [],
    events,
    artifacts: [],
    has_pending_tool_calls: false,
    run_started_at: "2026-09-15T00:00:00Z",
    active_run_id: "run1",
  };
}

test("乐观用户气泡在后端回放同一条指令时保持同一个 id，不会被拆掉重挂", () => {
  const aggregator = new SessionStreamAggregator();
  aggregator.addOptimisticUserMessage("帮我检索论文", "turn-1");
  const optimisticId = aggregator.snapshot().messages[0]?.id;

  const applied = aggregator.apply(
    runtimeEvent({
      event: "message",
      role: "user",
      content: "帮我检索论文",
      turn_id: "turn-1",
      stream_seq: 1,
    }),
  );

  const snapshot = aggregator.snapshot();
  assert.equal(applied, false);
  assert.equal(snapshot.messages.length, 1);
  assert.equal(snapshot.messages[0]?.id, optimisticId);
});

test("相同 stream_seq 的增量再次到达时不重复拼接正文", () => {
  const aggregator = new SessionStreamAggregator();
  aggregator.apply(
    runtimeEvent({
      event: "delta",
      content: "你好",
      turn_id: "turn-1",
      stream_seq: 2,
    }),
  );
  const first = aggregator.snapshot();
  const assistantId = first.messages[0]?.id;
  const appliedAgain = aggregator.apply(
    runtimeEvent({
      event: "delta",
      content: "你好",
      turn_id: "turn-1",
      stream_seq: 2,
    }),
  );

  const snapshot = aggregator.snapshot();
  assert.equal(appliedAgain, false);
  assert.equal(snapshot.messages.length, 1);
  assert.equal(snapshot.messages[0]?.id, assistantId);
  assert.equal(snapshot.messages[0]?.content, "你好");
});

test("hydrate 之后再重放同一批 SSE 历史，不会复制气泡或工具卡片", () => {
  const events: StoredSessionEvent[] = [
    stored({
      event_type: "message",
      seq_no: 1,
      content: "帮我检索论文",
      metadata: {
        event: "message",
        role: "user",
        content: "帮我检索论文",
        turn_id: "turn-1",
        stream_seq: 1,
      },
    }),
    stored({
      event_type: "delta",
      seq_no: 2,
      content: "正在检索",
      metadata: {
        event: "delta",
        content: "正在检索",
        turn_id: "turn-1",
        stream_seq: 2,
      },
    }),
    stored({
      event_type: "runtime_event",
      seq_no: 3,
      content: "正在执行 search_papers",
      metadata: {
        event: "runtime_event",
        id: "turn-1:tool:search_papers_1",
        parent_id: "turn-1:tool",
        title: "检索论文",
        status: "running",
        show_content: "正在执行 search_papers",
        turn_id: "turn-1",
        stream_seq: 3,
      },
    }),
  ];

  const aggregator = new SessionStreamAggregator();
  aggregator.hydrate(runningThread(events));
  const before = aggregator.snapshot();

  for (const event of events) {
    aggregator.apply({
      ...(event.metadata as SessionRuntimeEvent),
      event: event.event_type,
      session_key: "s",
      stream_seq: event.seq_no,
    });
  }

  const after = aggregator.snapshot();
  assert.equal(after.messages.length, before.messages.length);
  assert.equal(after.messages[0]?.id, before.messages[0]?.id);
  assert.equal(after.messages[1]?.content, "正在检索");
  assert.equal(after.runtimeEvents.length, before.runtimeEvents.length);
});

test("hydrate 回放用户消息时复用和乐观插入相同的稳定 id", () => {
  const aggregator = new SessionStreamAggregator();
  aggregator.addOptimisticUserMessage("帮我检索论文", "turn-1");
  const liveId = aggregator.snapshot().messages[0]?.id;

  aggregator.hydrate(
    runningThread([
      stored({
        event_type: "message",
        seq_no: 1,
        content: "帮我检索论文",
        metadata: {
          event: "message",
          role: "user",
          content: "帮我检索论文",
          turn_id: "turn-1",
          stream_seq: 1,
        },
      }),
    ]),
  );

  assert.equal(aggregator.snapshot().messages[0]?.id, liveId);
});

test("思考增量把 stream_seq 抬高后，序号更小的工具完成和 turn_end 仍要落地", () => {
  const aggregator = new SessionStreamAggregator();
  aggregator.apply(
    runtimeEvent({
      event: "runtime_event",
      id: "turn-1:tool:expand_by_citations_1",
      parent_id: "turn-1:tool",
      title: "引文扩展检索",
      status: "running",
      show_content: "正在执行 expand_by_citations",
      turn_id: "turn-1",
      stream_seq: 240,
    }),
  );
  aggregator.apply(
    runtimeEvent({
      event: "reasoning_delta",
      content: "先顺着引用关系扩展",
      turn_id: "turn-1",
      stream_seq: 1200,
    }),
  );

  const completed = aggregator.apply(
    runtimeEvent({
      event: "runtime_event",
      id: "turn-1:tool:expand_by_citations_1",
      parent_id: "turn-1:tool",
      title: "引文扩展检索",
      status: "completed",
      show_content: "工具执行已完成",
      turn_id: "turn-1",
      stream_seq: 242,
    }),
  );
  const ended = aggregator.apply(
    runtimeEvent({
      event: "turn_end",
      status: "completed",
      turn_id: "turn-1",
      stream_seq: 262,
    }),
  );

  const snapshot = aggregator.snapshot();
  const card = snapshot.runtimeEvents[0]?.children[0];
  assert.equal(completed, true);
  assert.equal(ended, true);
  assert.equal(card?.status, "completed");
  assert.equal(snapshot.status, "completed");
  assert.equal(snapshot.isStreaming, false);
});

test("会话已结束后 hydrate 不会把未收到完成事件的工具卡留在处理中", () => {
  const aggregator = new SessionStreamAggregator();
  aggregator.hydrate({
    key: "s",
    title: "调研对话",
    status: "completed",
    messages: [],
    events: [
      stored({
        event_type: "runtime_event",
        seq_no: 240,
        content: "正在执行 expand_by_citations",
        metadata: {
          event: "runtime_event",
          id: "turn-1:tool:expand_by_citations_1",
          parent_id: "turn-1:tool",
          title: "引文扩展检索",
          status: "running",
          show_content: "正在执行 expand_by_citations",
          turn_id: "turn-1",
          stream_seq: 240,
        },
      }),
      stored({
        event_type: "turn_end",
        seq_no: 262,
        metadata: {
          event: "turn_end",
          status: "completed",
          turn_id: "turn-1",
          stream_seq: 262,
        },
      }),
    ],
    artifacts: [],
    has_pending_tool_calls: false,
    run_started_at: null,
    active_run_id: null,
  });

  const snapshot = aggregator.snapshot();
  const card = snapshot.runtimeEvents[0]?.children[0];
  assert.equal(snapshot.status, "completed");
  assert.equal(snapshot.isStreaming, false);
  assert.equal(card?.status, "completed");
});
