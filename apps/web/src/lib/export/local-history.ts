/** Persists locally rendered videos in project-scoped browser storage. */
import type { RenderSpecV2 } from "../api/types";

export interface LocalRenderRecord {
  id: string;
  projectId: string;
  createdAt: string;
  resolution: RenderSpecV2["resolution"];
  isPreview: boolean;
  blob: Blob;
}

const DATABASE_NAME = "trip-animator-local-renders";
const DATABASE_VERSION = 1;
const STORE_NAME = "renders";
const PROJECT_INDEX = "projectId";

let databasePromise: Promise<IDBDatabase> | undefined;

function openDatabase(): Promise<IDBDatabase> {
  if (typeof indexedDB === "undefined") {
    return Promise.reject(new Error("IndexedDB is unavailable in this browser."));
  }
  if (!databasePromise) {
    databasePromise = new Promise<IDBDatabase>((resolve, reject) => {
      const request = indexedDB.open(DATABASE_NAME, DATABASE_VERSION);
      request.onupgradeneeded = () => {
        const database = request.result;
        if (!database.objectStoreNames.contains(STORE_NAME)) {
          const store = database.createObjectStore(STORE_NAME, { keyPath: "id" });
          store.createIndex(PROJECT_INDEX, "projectId", { unique: false });
        }
      };
      request.onsuccess = () => {
        const database = request.result;
        database.onversionchange = () => {
          database.close();
          databasePromise = undefined;
        };
        resolve(database);
      };
      request.onerror = () => reject(request.error ?? new Error("Could not open local render history."));
    }).catch((error: unknown) => {
      databasePromise = undefined;
      throw error;
    });
  }
  return databasePromise;
}

function transactionComplete(transaction: IDBTransaction): Promise<void> {
  return new Promise((resolve, reject) => {
    transaction.oncomplete = () => resolve();
    transaction.onabort = () => reject(transaction.error ?? new Error("Local render history transaction aborted."));
    transaction.onerror = () => reject(transaction.error ?? new Error("Local render history transaction failed."));
  });
}

/** Lists the saved videos belonging to one project, newest first. */
export async function listLocalRenders(projectId: string): Promise<LocalRenderRecord[]> {
  const database = await openDatabase();
  const transaction = database.transaction(STORE_NAME, "readonly");
  const completed = transactionComplete(transaction);
  const request = transaction.objectStore(STORE_NAME).index(PROJECT_INDEX).getAll(projectId);
  await completed;
  return (request.result as LocalRenderRecord[]).sort((left, right) =>
    right.createdAt.localeCompare(left.createdAt),
  );
}

/** Saves a video record atomically to browser storage. */
export async function saveLocalRender(record: LocalRenderRecord): Promise<void> {
  const database = await openDatabase();
  const transaction = database.transaction(STORE_NAME, "readwrite");
  const completed = transactionComplete(transaction);
  transaction.objectStore(STORE_NAME).put(record);
  await completed;
}
