import { Prism as SyntaxHighlighter } from 'react-syntax-highlighter';
import { oneDark } from 'react-syntax-highlighter/dist/esm/styles/prism';

const customTheme = {
  ...oneDark,
  'pre[class*="language-"]': {
    ...oneDark['pre[class*="language-"]'],
    background: '#0f1117',
    margin: '8px 0',
    borderRadius: '8px',
    fontSize: '13px',
    border: '1px solid #2a2e3f',
  },
  'code[class*="language-"]': {
    ...oneDark['code[class*="language-"]'],
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
