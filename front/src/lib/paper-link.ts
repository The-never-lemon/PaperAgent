/**
 * 论文编号对照，以及「原文」链接怎么挑。
 *
 * 中文说明：
 * 助手回复里的 [编号] 经常不是工作区主键的原样拷贝——DOI 大小写不同、
 * 写成了 arXiv DOI、或用了数据源自己的网址。这里把这些写法收成同一组
 * 对照键，对上了就能点。
 *
 * 「原文」按钮优先给真正的 PDF 或检索源页面；实在没有时再用 DOI 跳转，
 * 保证每篇尽量都有一个能点开的地址。
 */

/** DOI 跳转地址的长相：doi.org 或 dx.doi.org。 */
const DOI_LINK_PATTERN = /^https?:\/\/(?:dx\.|www\.)?doi\.org\//i;

/** 正规 DOI 的长相：以 10. 开头，后面有登记机构号和一条斜杠。 */
const DOI_ID_PATTERN = /^10\.\d{4,9}\/\S+$/i;

/** 2007 年之后的 arXiv 编号，例如 2401.12345 或 2401.12345v3。 */
const ARXIV_ID_PATTERN = /^\d{4}\.\d{4,5}(?:v\d+)?$/i;

/** Semantic Scholar 常用的 40 位十六进制编号。 */
const SEMANTIC_SCHOLAR_ID_PATTERN = /^[a-f0-9]{40}$/i;

export type PaperRefLookup = Map<string, string>;

export type PaperLinkSource = {
  paper_id?: string;
  doi?: string;
  url?: string;
  pdf_url?: string;
};

/** 判断一个地址是不是 DOI 跳转地址。 */
export function isDoiLink(url: string | null | undefined) {
  return DOI_LINK_PATTERN.test((url || "").trim());
}

/**
 * 把一个编号展开成对照用的各种写法。
 *
 * 中文说明：同一篇论文可能被写成 10.1109/LRA.xxx、doi:10.1109/lra.xxx、
 * 10.48550/arXiv.2401.12345、arxiv:2401.12345。这里把它们收成小写、去掉网址前缀，
 * 方便和和工作区主键对上。
 */
export function expandPaperRefKeys(raw: string | null | undefined): string[] {
  const text = String(raw || "").trim();
  if (!text) {
    return [];
  }
  const keys = new Set<string>();
  const add = (value: string) => {
    const cleaned = value.trim();
    if (!cleaned) {
      return;
    }
    keys.add(cleaned);
    keys.add(cleaned.toLowerCase());
  };

  add(text);
  let stripped = text
    .replace(DOI_LINK_PATTERN, "")
    .replace(/^https?:\/\/arxiv\.org\/(?:abs|pdf)\//i, "")
    .replace(/\.pdf$/i, "")
    .replace(/^doi:/i, "")
    .replace(/\/+$/, "");
  add(stripped);

  const doi = normalizeDoiToken(stripped);
  if (doi) {
    add(doi);
    add(`doi:${doi}`);
    if (doi.startsWith("10.48550/arxiv.")) {
      addArxivKeys(keys, doi.slice("10.48550/arxiv.".length));
    }
  }

  const lowered = stripped.toLowerCase();
  if (lowered.startsWith("arxiv:")) {
    addArxivKeys(keys, lowered.slice("arxiv:".length));
  } else if (ARXIV_ID_PATTERN.test(lowered)) {
    addArxivKeys(keys, lowered);
  }

  const openAlexUrl = text.match(/openalex\.org\/(W\d+)/i);
  if (openAlexUrl?.[1]) {
    add(openAlexUrl[1]);
  } else if (/^W\d+$/i.test(stripped)) {
    add(stripped);
  }

  const s2Url = text.match(/semanticscholar\.org\/paper\/([a-f0-9]{40})/i);
  if (s2Url?.[1]) {
    add(s2Url[1]);
  } else if (SEMANTIC_SCHOLAR_ID_PATTERN.test(stripped.toLowerCase())) {
    add(stripped.toLowerCase());
  }

  return [...keys];
}

/** 把一篇论文的主键和它带的 DOI / 网址登记进对照表。 */
export function addPaperRefAliases(
  lookup: PaperRefLookup,
  canonicalId: string,
  extras: Array<string | null | undefined> = [],
) {
  const paperId = canonicalId.trim();
  if (!paperId) {
    return;
  }
  for (const token of [paperId, ...extras]) {
    for (const key of expandPaperRefKeys(token)) {
      if (!lookup.has(key)) {
        lookup.set(key, paperId);
      }
    }
  }
}

/** 方括号里的文字若能对上工作区里的某篇论文，就返回那篇的主键。 */
export function resolvePaperRef(
  candidate: string,
  lookup: PaperRefLookup | undefined,
): string | null {
  if (!lookup || lookup.size === 0) {
    return null;
  }
  for (const key of expandPaperRefKeys(candidate)) {
    const hit = lookup.get(key);
    if (hit) {
      return hit;
    }
  }
  return null;
}

/**
 * 挑出能给用户打开的论文地址。
 *
 * 中文说明：先用开放获取 PDF 和检索源页面（arXiv / OpenAlex / Semantic Scholar）；
 * 这两样都没有时，再根据编号拼一个地址——arXiv 摘要页、DOI 跳转、OpenAlex 页面、
 * Semantic Scholar 页面。拼不出来才返回空字符串，按钮才隐藏。
 */
export function pickPaperLink(paper: PaperLinkSource) {
  const preferred = [paper.pdf_url, paper.url]
    .map((value) => (value || "").trim())
    .filter(Boolean)
    .filter((value) => !isDoiLink(value));
  if (preferred[0]) {
    return preferred[0];
  }

  const synthesized = synthesizePaperLink(paper);
  if (synthesized) {
    return synthesized;
  }

  const fallback = [paper.pdf_url, paper.url]
    .map((value) => (value || "").trim())
    .filter(Boolean);
  return fallback[0] || "";
}

function synthesizePaperLink(paper: PaperLinkSource) {
  const tokens = [paper.paper_id, paper.doi, paper.url, paper.pdf_url]
    .map((value) => String(value || "").trim())
    .filter(Boolean);

  // 中文说明：arXiv 论文优先打开摘要页，比 DOI 跳转更好读。
  for (const text of tokens) {
    const arxivId = extractArxivId(text);
    if (arxivId) {
      return `https://arxiv.org/abs/${arxivId}`;
    }
  }

  for (const text of tokens) {
    if (/^https?:\/\//i.test(text) && !isDoiLink(text)) {
      return text;
    }
  }

  for (const text of tokens) {
    const openAlex = /^(?:https?:\/\/(?:www\.)?openalex\.org\/)?(W\d+)$/i.exec(text);
    if (openAlex?.[1]) {
      return `https://openalex.org/${openAlex[1]}`;
    }
  }

  for (const text of tokens) {
    const s2 = /^(?:https?:\/\/(?:www\.)?semanticscholar\.org\/paper\/)?([a-f0-9]{40})$/i.exec(text);
    if (s2?.[1]) {
      return `https://www.semanticscholar.org/paper/${s2[1]}`;
    }
  }

  for (const text of tokens) {
    const doi = normalizeDoiToken(text);
    // 中文说明：只有真的像 DOI 才去拼 doi.org，避免把本地上传的哈希编号拼成假地址。
    if (isLikelyDoi(doi) && !doi.startsWith("10.48550/arxiv.")) {
      return `https://doi.org/${doi}`;
    }
  }

  return "";
}

function addArxivKeys(keys: Set<string>, rawId: string) {
  const arxivId = stripArxivVersion(rawId);
  if (!arxivId) {
    return;
  }
  keys.add(arxivId);
  keys.add(`arxiv:${arxivId}`);
  keys.add(`10.48550/arxiv.${arxivId}`);
  keys.add(`doi:10.48550/arxiv.${arxivId}`);
}

function extractArxivId(raw: string) {
  const stripped = raw
    .trim()
    .replace(DOI_LINK_PATTERN, "")
    .replace(/^https?:\/\/arxiv\.org\/(?:abs|pdf)\//i, "")
    .replace(/\.pdf$/i, "")
    .replace(/^doi:/i, "");
  const doi = normalizeDoiToken(stripped);
  if (doi.startsWith("10.48550/arxiv.")) {
    return stripArxivVersion(doi.slice("10.48550/arxiv.".length));
  }
  const lowered = stripped.toLowerCase().replace(/^arxiv:/, "");
  if (ARXIV_ID_PATTERN.test(lowered)) {
    return stripArxivVersion(lowered);
  }
  return "";
}

function normalizeDoiToken(raw: string) {
  return raw
    .trim()
    .replace(DOI_LINK_PATTERN, "")
    .replace(/^doi:/i, "")
    .toLowerCase();
}

/** 判断整理后的字符串是不是正规 DOI。 */
function isLikelyDoi(value: string) {
  return DOI_ID_PATTERN.test(value);
}

function stripArxivVersion(value: string) {
  return value.trim().replace(/v\d+$/i, "");
}
