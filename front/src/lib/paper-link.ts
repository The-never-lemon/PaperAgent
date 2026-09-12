/**
 * 论文对外链接的挑选规则。
 *
 * 中文说明：
 * 数据源给的 pdf_url 并不总是真正的 PDF 文件。OpenAlex 在找不到 PDF 时，
 * 会把"开放获取位置"里的地址直接填进来，而很多出版社在这个字段里给的是
 * DOI 跳转地址（形如 https://doi.org/10.xxxx/yyyy）。
 *
 * DOI 地址有两个问题：
 * 1. 点开不是论文本身，而是跳到一个落地页；
 * 2. 浏览器上装了 sci-hub 之类的插件时，DOI 地址会被插件劫持到第三方站点。
 *
 * 所以给用户看的"原文"链接要跳过 DOI 地址，改用检索源自己的论文页面
 * （arXiv 摘要页、OpenAlex 详情页、Semantic Scholar 详情页）。
 */

/** DOI 跳转地址的长相：doi.org 或 dx.doi.org。 */
const DOI_LINK_PATTERN = /^https?:\/\/(?:dx\.)?doi\.org\//i;

/** 判断一个地址是不是 DOI 跳转地址。 */
export function isDoiLink(url: string | null | undefined) {
  return DOI_LINK_PATTERN.test((url || "").trim());
}

/**
 * 挑出能给用户打开的论文地址。
 *
 * 中文说明：先试 pdf_url（多为 arXiv 或开放获取的真实 PDF），
 * 它是 DOI 跳转地址就跳过；再试 url（检索源里的论文页面）；
 * 两个都不合适时返回空字符串，调用方据此把链接按钮隐藏掉。
 */
export function pickPaperLink(paper: { url?: string; pdf_url?: string }) {
  const candidates = [paper.pdf_url, paper.url]
    .map((value) => (value || "").trim())
    .filter(Boolean);
  return candidates.find((value) => !isDoiLink(value)) || "";
}
