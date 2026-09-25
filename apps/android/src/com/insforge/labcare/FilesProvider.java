package com.insforge.labcare;

import android.content.ContentProvider;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.ParcelFileDescriptor;

import java.io.File;
import java.io.FileNotFoundException;

/**
 * Minimal single-directory FileProvider replacement — this build has no
 * androidx, so files saved from the in-app site (e.g. PDF service reports)
 * are shared to the system viewer through this content:// provider instead
 * of a file:// URI.
 */
public class FilesProvider extends ContentProvider {

    static final String AUTHORITY = "com.insforge.labcare.files";

    static File baseDir(Context ctx) {
        File dir = ctx.getExternalFilesDir(android.os.Environment.DIRECTORY_DOWNLOADS);
        return dir != null ? dir : ctx.getFilesDir();
    }

    static Uri uriFor(Context ctx, File f) {
        return new Uri.Builder().scheme("content").authority(AUTHORITY)
                .appendPath(f.getName()).build();
    }

    @Override
    public boolean onCreate() {
        return true;
    }

    private File resolve(Uri uri) throws FileNotFoundException {
        Context ctx = getContext();
        if (ctx == null) throw new FileNotFoundException("no context");
        String name = uri.getLastPathSegment();
        if (name == null) throw new FileNotFoundException("bad uri");
        File f = new File(baseDir(ctx), new File(name).getName());
        if (!f.exists()) throw new FileNotFoundException(name);
        return f;
    }

    @Override
    public ParcelFileDescriptor openFile(Uri uri, String mode) throws FileNotFoundException {
        int m = ParcelFileDescriptor.parseMode(mode == null || mode.isEmpty() ? "r" : mode);
        return ParcelFileDescriptor.open(resolve(uri), m);
    }

    @Override
    public String getType(Uri uri) {
        return "application/octet-stream";
    }

    @Override
    public Cursor query(Uri uri, String[] projection, String selection,
                        String[] selectionArgs, String sortOrder) {
        return null;
    }

    @Override
    public Uri insert(Uri uri, ContentValues values) {
        return null;
    }

    @Override
    public int delete(Uri uri, String selection, String[] selectionArgs) {
        return 0;
    }

    @Override
    public int update(Uri uri, ContentValues values, String selection,
                      String[] selectionArgs) {
        return 0;
    }
}
