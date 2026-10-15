// Brand configuration — single source of truth for product naming.
// Override at build time via Vite env vars
// (VITE_BRAND_NAME / VITE_BRAND_TAGLINE / VITE_BRAND_DESCRIPTION).
//
// Source code references BRAND.name (or the [[brand]] placeholder in i18n
// resources) so the displayed product name is not hardcoded across files.

const env = import.meta.env as Record<string, string | undefined>;

function fallback(value: string | undefined, defaultValue: string): string {
  if (typeof value !== "string") return defaultValue;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : defaultValue;
}

export const BRAND = {
  name: fallback(env.VITE_BRAND_NAME, "Studio360"),
  tagline: fallback(env.VITE_BRAND_TAGLINE, "Nền tảng sáng tạo video AI"),
  description: fallback(
    env.VITE_BRAND_DESCRIPTION,
    "Nền tảng sáng tạo video AI, quản lý thống nhất dự án, kịch bản, phân cảnh, tạo video và tương tác trợ lý thông minh.",
  ),
} as const;

export const BRAND_DOCUMENT_TITLE = `${BRAND.name} · ${BRAND.tagline}`;
