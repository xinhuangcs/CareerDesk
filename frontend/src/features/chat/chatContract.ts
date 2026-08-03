export type ChatTurnState = "absent" | "running" | "completed" | "unknown" | "cancelled";

export type ChatProposalSurface =
  | "intake"
  | "application_merge"
  | "application_delete"
  | "review_undo";

export type ChatProposalOperationReference = {
  surface: ChatProposalSurface;
  operation_id: string;
};

export type ChatTurnStatus = {
  client_turn_id: string;
  state: ChatTurnState;
  terminal: boolean;
  proposal_operations: ChatProposalOperationReference[];
};

export type ChatRecoveryScope = {
  scope: string;
};

export type Attachment =
  | {
      status: "ok";
      kind: "document";
      filename: string;
      text: string;
      truncated?: boolean;
    }
  | {
      status: "ok";
      kind: "image";
      filename: string;
      stored: string;
    };

export type AttachmentUploadResponse =
  | Attachment
  | { status: "error"; message?: string };
