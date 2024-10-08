/*
 * Copyright (C) 2019 The Android Open Source Project
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *   http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */
package androidx.test.services.storage.provider;

import static androidx.test.internal.util.Checks.checkNotNull;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.database.MatrixCursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;
import android.util.Log;
import android.webkit.MimeTypeMap;
import androidx.test.services.storage.file.HostedFile;
import java.io.File;
import java.io.FileNotFoundException;
import java.io.IOException;

/**
 * Content provider that allows access to reading and (optionally) writing files.
 *
 * <p>This is used to expose readonly copies of tests data dependencies and also provides a
 * standardized way of exposing test output.
 *
 * <p>By placing these IO activities inside a content provider that is installed as an APK separate
 * from the test apks, we ensure that the test or app doesn't need any extra permissions such as
 * WRITE_EXTERNAL_STORAGE.
 */
abstract class AbstractFileContentProvider extends ContentProvider {
  private static final String TAG = AbstractFileContentProvider.class.getSimpleName();

  private File hostedDirectory;
  private Access access;

  enum Access {
    READ_ONLY,
    READ_WRITE
  }

  /** Returns the root directory where this content provider should manage files from */
  protected abstract File getHostedDirectory(Context context);

  /** Returns the access mode of this content provider */
  protected abstract Access getAccess();

  @Override
  public boolean onCreate() {
    hostedDirectory = getHostedDirectory(getContext());
    access = getAccess();
    return true;
  }

  @Override
  public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
    checkNotNull(uri);
    checkNotNull(mode);
    String lowerMode = mode.toLowerCase();
    boolean callWillWrite = lowerMode.contains("w") || lowerMode.contains("t");

    if ((Access.READ_ONLY == access) && callWillWrite) {
      throw new SecurityException(
          String.format("Location '%s' is read only (Requested mode: '%s')", uri, lowerMode));
    }
    File requestedFile = fromUri(uri);
    if (!requestedFile.exists() && callWillWrite) {
      try {
        requestedFile.getParentFile().mkdirs();
        if (!requestedFile.getParentFile().exists()) {
          Log.e(
              TAG,
              "Failed to create parent directory "
                  + requestedFile.getParentFile().getAbsolutePath());
          throw new FileNotFoundException(String.format("No parent directory for '%s'", uri));
        }

        if (!requestedFile.createNewFile()) {
          Log.e(TAG, "Failed to create file" + requestedFile.getAbsolutePath());
          throw new FileNotFoundException("Could not create file: " + uri);
        }
      } catch (IOException ioe) {
        throw new FileNotFoundException(
            String.format("Could not access file: %s Exception: %s", uri, ioe.getMessage()));
      }
    }
    Log.i(
        TAG,
        String.format(
            "file '%s': %s", requestedFile, requestedFile.exists() ? "found" : "not found"));
    return openFileHelper(uri, mode);
  }

  private File fromUri(Uri inUri) throws FileNotFoundException {
    File requestedFile = null;
    try {
      requestedFile = new File(hostedDirectory, inUri.getPath()).getCanonicalFile();
    } catch (IOException ioe) {
      throw new FileNotFoundException(
          String.format(
              "'%s': error resolving to canonical path - %s", requestedFile, ioe.getMessage()));
    }

    File checkFile = requestedFile.getAbsoluteFile();
    while (null != checkFile) {
      if (checkFile.equals(hostedDirectory)) {
        return requestedFile;
      }
      checkFile = checkFile.getParentFile();
    }

    // Hmm... our requested file is not under the expected parent directory.
    throw new SecurityException(
        String.format("URI '%s' refers to a file not managed by this provider", inUri));
  }

  @Override
  public Cursor query(
      Uri uri, String[] projection, String selection, String[] selectionArgs, String sortOrder) {

    File requestedFile = null;
    try {
      requestedFile = fromUri(uri);
    } catch (FileNotFoundException fnfe) {
      Log.w(TAG, "could not find file for query.", fnfe);
      throw new RuntimeException(fnfe);
    }

    File[] children = requestedFile.listFiles();
    String[] cols = HostedFile.HostedFileColumn.getColumnNames();
    if (null != children) {
      MatrixCursor cursor = new MatrixCursor(cols, children.length);
      for (File child : children) {
        MatrixCursor.RowBuilder row = cursor.newRow();
        row.add(uri.getPath() + "/" + Uri.encode(child.getName()));
        if (child.isDirectory()) {
          row.add(HostedFile.FileType.DIRECTORY.getTypeCode());
          row.add(child.listFiles().length);
        } else {
          row.add(HostedFile.FileType.FILE.getTypeCode());
          row.add(child.length());
        }
        row.add(child.getAbsolutePath());
        row.add(child.getName());
        row.add(child.length());
      }
      return cursor;
    } else if (requestedFile.exists()) {
      MatrixCursor cursor = new MatrixCursor(cols, 1);
      MatrixCursor.RowBuilder row = cursor.newRow();
      row.add(uri.getPath());
      row.add(HostedFile.FileType.FILE.getTypeCode());
      row.add(requestedFile.length());
      row.add(requestedFile.getAbsolutePath());
      row.add(requestedFile.getName());
      row.add(requestedFile.length());
      return cursor;
    } else {
      Log.i(
          TAG,
          String.format(
              "%s: does not exist. Mapped from uri: '%s'", requestedFile.getAbsolutePath(), uri));
      return new MatrixCursor(cols, 0);
    }
  }

  @Override
  public int update(Uri uri, ContentValues values, String selection, String[] selectionArgs) {
    // not allowed.
    return 0;
  }

  @Override
  public int delete(Uri uri, String selection, String[] selectionArgs) {
    Log.d(TAG, "Deleting hosted file " + uri);
    try {
      File requestedFile = fromUri(uri);
      // Only deleting the entire hosted directory is supported. Also, the root hosted directory
      // will be preserved to avoid recreating between test cases.
      if (hostedDirectory.equals(requestedFile)) {
        for (File child : hostedDirectory.listFiles()) {
          deleteRecursively(child);
        }
      } else {
        throw new StorageContentProviderException(
            "Deleting file/directory other than the entire hosted directory is not supported!");
      }
    } catch (FileNotFoundException fnfe) {
      Log.w(TAG, "Could not find file for query.", fnfe);
      throw new StorageContentProviderException("Hosted file " + uri + " was not found!", fnfe);
    }
    return 0;
  }

  @Override
  public String getType(Uri uri) {
    checkNotNull(uri);
    // Takes a wild guess at the mime type by looking for the file extension.
    String extension = MimeTypeMap.getFileExtensionFromUrl(uri.toString());
    MimeTypeMap map = MimeTypeMap.getSingleton();
    return map.getMimeTypeFromExtension(extension);
  }

  @Override
  public Uri insert(Uri uri, ContentValues contentValues) {
    throw new UnsupportedOperationException("Insertion is not allowed.");
  }

  // @Override since api 11
  public void shutdown() {
    // no open services, this just suppresses a logger warning.
  }

  /** Deletes the file or directory recursively. */
  private void deleteRecursively(File fileOrDirectory) {
    if (fileOrDirectory.isDirectory()) {
      for (File child : fileOrDirectory.listFiles()) {
        deleteRecursively(child);
      }
    }
    fileOrDirectory.delete();
  }
}
