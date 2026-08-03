import { del, getJson, postJson } from "../../shared/api/transport.ts";

export type StorageLocationState = {
  data_dir: string;
  config_dir: string;
  log_dir: string;
  uses_default_data_dir: boolean;
  can_customize: boolean;
  customization_issue: string | null;
  migration_pending: string | null;
  migration_issue: string | null;
  credential_storage_kind: "system" | "configuration_file" | "server_environment";
  credential_location: string;
};

export function loadStorageLocation(): Promise<StorageLocationState> {
  return getJson<StorageLocationState>("/api/settings/storage");
}

export function claimStorageDisclosure(): Promise<{ should_show: boolean }> {
  return postJson<{ should_show: boolean }>("/api/settings/storage-disclosure/claim", {});
}

export function revealStorageLocation(
  target: "data" | "config" | "logs",
): Promise<StorageLocationState> {
  return postJson<StorageLocationState>("/api/settings/storage/reveal", { target });
}

export function requestStorageMigration(destination: string): Promise<StorageLocationState> {
  return postJson<StorageLocationState>("/api/settings/storage/migration", { destination });
}

export function cancelStorageMigration(): Promise<StorageLocationState> {
  return del<StorageLocationState>("/api/settings/storage/migration");
}
