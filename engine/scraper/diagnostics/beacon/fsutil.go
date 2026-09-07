package main

import (
	"archive/tar"
	"compress/gzip"
	"io"
	"os"
	"path/filepath"
)

func tarGzDir(srcDir string, w io.Writer) error {
	gw := gzip.NewWriter(w)
	defer gw.Close()

	tw := tar.NewWriter(gw)
	defer tw.Close()

	return filepath.Walk(srcDir, func(path string, info os.FileInfo, err error) error {
		if err != nil {
			return nil // skip errors
		}

		relPath, err := filepath.Rel(srcDir, path)
		if err != nil {
			return nil
		}

		header, err := tar.FileInfoHeader(info, "")
		if err != nil {
			return nil
		}
		header.Name = relPath

		if err := tw.WriteHeader(header); err != nil {
			return err
		}

		if info.IsDir() {
			return nil
		}

		f, err := os.Open(path)
		if err != nil {
			return nil // skip unreadable files
		}

		_, copyErr := io.Copy(tw, f)
		f.Close() // explicit close instead of defer in Walk callback
		return copyErr
	})
}
