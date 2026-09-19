// Curated starting points for the setup UI. Every field remains editable so
// private gateways, newer model IDs, and companies outside this list work too.
export const MODEL_PRESETS = [
  {
    value: 'deepseek-flash',
    label: 'DeepSeek Flash',
    provider: 'openai_compatible',
    base_url: 'https://api.deepseek.com',
  },
  {
    value: 'deepseek-v4-pro',
    label: 'DeepSeek V4 Pro',
    provider: 'openai_compatible',
    base_url: 'https://api.deepseek.com',
  },
  {
    value: 'gpt-4o-mini',
    label: 'OpenAI GPT-4o mini',
    provider: 'openai',
    base_url: 'https://api.openai.com/v1',
  },
]

export const COMPANY_PRESETS = [
  { value: '600519 贵州茅台', label: '600519 · 贵州茅台' },
  { value: '000858 五粮液', label: '000858 · 五粮液' },
  { value: '300750 宁德时代', label: '300750 · 宁德时代' },
  { value: '601318 中国平安', label: '601318 · 中国平安' },
  { value: '000001 平安银行', label: '000001 · 平安银行' },
  { value: '603893 瑞芯微', label: '603893 · 瑞芯微' },
]
