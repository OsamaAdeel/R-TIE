import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneLight } from 'react-syntax-highlighter/dist/esm/styles/prism';

const customTheme = {
  ...oneLight,
  'pre[class*="language-"]': {
    ...oneLight['pre[class*="language-"]'],
    background: '#f8fafc',
    margin: '8px 0',
    borderRadius: '12px',
    fontSize: '13px',
    border: '2px solid #e2e8f0',
  },
  'code[class*="language-"]': {
    ...oneLight['code[class*="language-"]'],
    background: 'transparent',
  },
};

export default function CodeBlock({ code, language = 'sql' }) {
  if (!code) return null;

  return (
    <SyntaxHighlighter
      language={language}
      style={customTheme}
      showLineNumbers
      wrapLongLines
    >
      {code.trim()}
    </SyntaxHighlighter>
  );
}
