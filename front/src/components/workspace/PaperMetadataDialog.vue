<script setup lang="ts">
/**
 * 上传论文后的信息确认弹窗。
 *
 * 中文说明：
 * 用户把本地 PDF 传上来之后，后端会尽量从 PDF 第一页认出标题、作者、年份和摘要。
 * 这个弹窗把那几项摆出来让用户核对，认错了可以当场改；不想改就直接关掉，
 * 已经存进去的内容不会变。改完点「保存」才会写回工作区。
 *
 * 如果后端发现这份 PDF 读不出文字（多半是扫描版），弹窗顶部会直接说明，
 * 免得用户点完精读、等半天，才拿到一份很空的报告还不知道为什么。
 */
import { onBeforeUnmount, ref, watch } from "vue";
import { Check, X } from "lucide-vue-next";

import { updatePaperMetadata, type UploadPaperResult } from "../../api/workspace";
import { pushToast } from "../../stores/notifications";

defineOptions({ name: "PaperMetadataDialog" });

const props = defineProps<{
  visible: boolean;
  sessionKey: string;
  /** 上传接口返回的核对信息，弹窗用它预填输入框。 */
  result: UploadPaperResult | null;
}>();

const emit = defineEmits<{
  close: [];
  /** 用户改完并保存成功。父组件收到后刷新论文列表。 */
  saved: [];
}>();

const title = ref("");
const authors = ref("");
const year = ref("");
const abstract = ref("");
const saving = ref(false);

// 中文注释：弹窗打开时挂上 ESC 关窗的监听，关闭后立刻摘掉，不让监听一直留在页面上。
// 顺便把后端认出来的信息填进输入框——每次上传都是一篇新的论文，不用保留上次的编辑。
watch(
  () => props.visible,
  (visible) => {
    if (!visible) {
      window.removeEventListener("keydown", onKeydown);
      return;
    }
    window.addEventListener("keydown", onKeydown);
    if (!props.result) return;
    title.value = props.result.title;
    authors.value = props.result.authors.join(", ");
    year.value = props.result.year ? String(props.result.year) : "";
    abstract.value = props.result.abstract;
  },
);

/** 把用户填的作者那一行文字，按逗号或分号切成一个个名字。 */
function splitAuthors(text: string): string[] {
  return text
    .split(/[,，;；]/)
    .map((part) => part.trim())
    .filter(Boolean);
}

async function save() {
  if (!props.result) return;
  saving.value = true;
  try {
    const rawYear = year.value.trim();
    await updatePaperMetadata(props.sessionKey, props.result.paper_id, {
      title: title.value.trim() || props.result.title,
      authors: splitAuthors(authors.value),
      // 中文注释：年份留空就不传，后端会把"没传"理解成"这一项不动"。
      year: rawYear ? Number(rawYear) : null,
      abstract: abstract.value.trim(),
    });
    pushToast({ tone: "success", title: "论文信息已保存" });
    emit("saved");
    emit("close");
  } catch (err) {
    pushToast({ tone: "error", title: "保存失败", description: (err as Error).message });
  } finally {
    saving.value = false;
  }
}

function onKeydown(event: KeyboardEvent) {
  if (event.key === "Escape") {
    emit("close");
  }
}

onBeforeUnmount(() => window.removeEventListener("keydown", onKeydown));
</script>

<template>
  <Teleport to="body">
    <div v-if="visible && result" class="paper-dialog-backdrop" @click.self="emit('close')">
      <section class="paper-dialog paper-meta-dialog" role="dialog" aria-modal="true" aria-label="确认论文信息">
        <header class="paper-dialog-head">
          <span class="paper-source-badge">已加入论文工作区</span>
          <button type="button" class="paper-dialog-close" aria-label="关闭" @click="emit('close')">
            <X :size="18" />
          </button>
        </header>

        <h3 class="paper-dialog-title">核对一下论文信息</h3>
        <p class="paper-meta-hint">
          下面这些是从 PDF 第一页自动认出来的，认错了可以直接改。不改也没关系，关掉即可。
        </p>

        <p v-if="result.has_text_layer === false" class="paper-meta-warning">
          这份 PDF 读不出文字（多半是扫描版或整页图片）。精读只能基于摘要，报告内容会非常有限。
        </p>

        <div class="paper-meta-form">
          <label class="paper-meta-field">
            <span class="paper-meta-label">标题</span>
            <input v-model="title" type="text" class="paper-meta-input" placeholder="论文标题" />
          </label>

          <label class="paper-meta-field">
            <span class="paper-meta-label">作者</span>
            <input
              v-model="authors"
              type="text"
              class="paper-meta-input"
              placeholder="多位作者用逗号隔开"
            />
          </label>

          <label class="paper-meta-field paper-meta-field-narrow">
            <span class="paper-meta-label">年份</span>
            <input v-model="year" type="text" inputmode="numeric" class="paper-meta-input" placeholder="2024" />
          </label>

          <label class="paper-meta-field">
            <span class="paper-meta-label">摘要</span>
            <textarea
              v-model="abstract"
              class="paper-meta-input paper-meta-textarea"
              rows="5"
              placeholder="没有自动提取到摘要，可以自己粘贴一段；不填也不影响精读。"
            ></textarea>
          </label>
        </div>

        <footer class="paper-dialog-actions">
          <button type="button" class="paper-card-action" :disabled="saving" @click="emit('close')">
            先这样
          </button>
          <button type="button" class="paper-card-action paper-card-action-primary" :disabled="saving" @click="save">
            <Check :size="14" /> {{ saving ? "保存中…" : "保存修改" }}
          </button>
        </footer>
      </section>
    </div>
  </Teleport>
</template>
