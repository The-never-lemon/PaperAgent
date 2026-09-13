<script setup lang="ts">
/**
 * 带公式的文字片段。
 *
 * 中文说明：
 * 精读报告的字段里可能夹着 LaTeX 公式，例如
 * 「总体损失为 $\mathcal{L}=\lambda_1\mathcal{L}_1+\lambda_2\mathcal{L}_2$」。
 * 这种文字当纯文本显示会看到一堆反斜杠和花括号，根本没法读。
 *
 * 这里做两件事：把文字按公式切开（切分逻辑在 lib/math-text.ts，是个纯函数，
 * 可以单独验证），然后普通文字原样显示、公式交给 MathFormula 排版。
 *
 * 注意这个组件没有单一根节点——它按顺序吐出一串「文字节点 + 公式节点」，
 * 所以外面用的时候直接放在 <p> / <li> / <span> 里就行，不要指望能往上加 class。
 */
import { computed } from "vue";

import { splitMathText, stripBareLatex } from "../../lib/math-text";
import MathFormula from "./MathFormula.vue";

defineOptions({ name: "MathText" });

const props = defineProps<{
  /** 要显示的文字，可能夹着 $...$ 形式的公式。 */
  text?: string | null;
}>();

const parts = computed(() =>
  splitMathText(props.text ?? "").map((part) =>
    // 普通文字那部分再剥一遍裸 LaTeX 标记（论文摘要里的 \textit{...}、\% 这类）。
    // 公式那部分不能碰，所以只处理 kind === "text" 的片段。
    part.kind === "text" ? { ...part, value: stripBareLatex(part.value) } : part
  )
);
</script>

<template>
  <template v-for="(part, index) in parts" :key="index">
    <MathFormula v-if="part.kind === 'math'" :tex="part.tex" :display="part.display" />
    <template v-else>{{ part.value }}</template>
  </template>
</template>
