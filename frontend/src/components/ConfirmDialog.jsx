import { AlertTriangle } from 'lucide-react';
import Modal from './Modal';

// Shared destructive-action confirmation, built on the Modal idiom. Used by
// both "Delete all chats" (SettingsMenu) and the per-conversation Delete
// (Sidebar) so every confirm reads identically — no native dialogs anywhere.
// Confirm is the ONLY path wired to the destructive action; Cancel / Escape /
// backdrop all dismiss without acting.
export default function ConfirmDialog({
  title,
  message,
  confirmLabel = 'Confirm',
  cancelLabel = 'Cancel',
  labelledBy = 'rtie-confirm-title',
  onCancel,
  onConfirm,
}) {
  return (
    <Modal labelledBy={labelledBy} onClose={onCancel}>
      <div className="flex items-start gap-3">
        <span className="shrink-0 mt-0.5 text-burgundy"><AlertTriangle size={18} /></span>
        <div className="min-w-0">
          <h2 id={labelledBy} className="text-[15px] font-semibold text-ivory">{title}</h2>
          {message && <p className="mt-1 text-[12.5px] text-ivory-dim">{message}</p>}
        </div>
      </div>
      <div className="mt-5 flex items-center justify-end gap-2">
        <button
          type="button"
          onClick={onCancel}
          className="px-3 py-1.5 rounded-md text-[12.5px] font-medium text-ivory-faint hover:text-ivory hover:bg-hover transition-colors"
        >
          {cancelLabel}
        </button>
        <button
          type="button"
          onClick={onConfirm}
          autoFocus
          // White text (not theme `ivory`, which is near-black in the light
          // theme) keeps AA contrast on the red fill in both themes
          // (~6.5:1 light / ~5.3:1 dark).
          className="px-3 py-1.5 rounded-md text-[12.5px] font-semibold text-white bg-burgundy hover:bg-burgundy/85 transition-colors"
        >
          {confirmLabel}
        </button>
      </div>
    </Modal>
  );
}
