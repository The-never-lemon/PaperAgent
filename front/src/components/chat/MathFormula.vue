<script setup lang="ts">
/**
 * 渲染一条公式。
 *
 * 中文说明：
 * 这个组件只干一件事——把一段 LaTeX 公式排版出来。它被两个地方复用：
 * 精读报告的字段（MathText.vue）和聊天消息（MarkdownText.vue）。
 *
 * 实现上有一处是刻意这么写的：**不使用 v-html**。
 * 模板里只有一个空的 <span>，KaTeX 把排版结果直接写进这个真实 DOM 节点。
 * 这么做有两个好处：
 *
 *   1. 项目一直坚持「不把模型输出当 HTML 塞进页面」这条性质，保住了。
 *      排版结果是 KaTeX 根据公式源码自己生成的，不是把原文当 HTML 执行；
 *      并且下面关掉了 trust，像 \href 这种能往页面里塞链接的命令会被挡掉。
 *
 *   2. 这个 <span> 在 Vue 眼里没有子节点，所以 Vue 重渲染时永远不会去动里面的内容。
 *      如果反过来——先让 Vue 渲染、再手动替换成公式——那就是在 Vue 正在管理的
 *      子树里做手术，文字一变两边必然打架。
 *
 * 而 KaTeX 本来就要求传一个真实的 DOM 节点给它，所以这个写法正好合适。
 */
import { onMounted, ref, watch } from "vue";
import katex from "katex";

import "katex/dist/katex.min.css";

defineOptions({ name: "MathFormula" });

const props = defineProps<{
  /** 公式源码，不带 $ 定界符。 */
  tex: string;
  /** 是否是独占一行的大公式。 */
  display?: boolean;
}>();

/** 承载排版结果的容器。 */
const host = ref<HTMLElement | null>(null);

/** 把公式画进容器里。KaTeX 会自己清掉上一次的内容。 */
function paint() {
  const element = host.value;
  if (!element) {
    return;
  }
  katex.render(props.tex, element, {
    displayMode: Boolean(props.display),
    // 公式写坏了（模型偶尔会漏半个括号）就把它显示成红色源码，
    // 不能让一条坏公式把整条消息变成打不开的空白。
    throwOnError: false,
    // 不给 \href 这类能往页面里插链接的命令开口子。
    trust: false,
  });
}

onMounted(paint);

// 文字是流式进来的，公式内容随时可能变，变了就重画。
watch([() => props.tex, () => props.display], paint);
</script>

<template>
  <span ref="host" class="math-formula"></span>
</template>
