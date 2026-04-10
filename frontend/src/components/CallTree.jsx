import { useState } from 'react';
import { ChevronRight, ChevronDown, Circle, AlertCircle, CheckCircle } from 'lucide-react';
import clsx from 'clsx';

export default function CallTree({ tree }) {
  if (!tree || !tree.dependencies) return null;

  return (
    <div className="bg-bg-tertiary rounded-lg p-3">
      <p className="text-xs text-text-muted mb-2 uppercase tracking-wider">Call Tree</p>
      <div className="font-mono text-sm">
        <TreeNode name={tree.root} isRoot />
        <div className="ml-4">
          {Object.entries(tree.dependencies).map(([name, info]) => (
            <TreeBranch key={name} name={name} info={info} depth={0} />
          ))}
        </div>
      </div>
    </div>
  );
}

function TreeBranch({ name, info, depth }) {
  const [expanded, setExpanded] = useState(depth < 2);
  const hasDeps = info?.dependencies && Object.keys(info.dependencies).length > 0;

  const statusIcon = {
    resolved: <CheckCircle size={10} className="text-success" />,
    not_found: <AlertCircle size={10} className="text-warning" />,
    circular_reference: <AlertCircle size={10} className="text-error" />,
    error: <AlertCircle size={10} className="text-error" />,
  };

  return (
    <div className={clsx('border-l border-border', depth > 0 && 'ml-3')}>
      <div
        onClick={() => hasDeps && setExpanded(!expanded)}
        className={clsx(
          'flex items-center gap-1.5 py-0.5 pl-2',
          hasDeps && 'cursor-pointer hover:text-accent'
        )}
      >
        {hasDeps ? (
          expanded ? <ChevronDown size={12} /> : <ChevronRight size={12} />
        ) : (
          <Circle size={6} className="text-text-muted ml-0.5 mr-0.5" />
        )}
        <span className="text-accent text-xs">{name}</span>
        {statusIcon[info?.status] || null}
        {info?.line_count && (
          <span className="text-xs text-text-muted ml-1">({info.line_count}L)</span>
        )}
      </div>
      {expanded && hasDeps && (
        <div className="ml-2">
          {Object.entries(info.dependencies).map(([n, i]) => (
            <TreeBranch key={n} name={n} info={i} depth={depth + 1} />
          ))}
        </div>
      )}
    </div>
  );
}

function TreeNode({ name, isRoot }) {
  return (
    <div className="flex items-center gap-1.5 py-0.5">
      <Circle size={8} className={clsx(isRoot ? 'text-accent fill-accent' : 'text-text-muted')} />
      <span className={clsx('text-xs', isRoot ? 'text-accent font-semibold' : 'text-text-secondary')}>
        {name}
      </span>
    </div>
  );
}
