"""Normalization: source XML -> canonical fragments -> silver Parquet.

``canonical_xml`` parses the WA/SA schema family (validate + load + daily
stitching); ``au_unixml`` and ``nsw_xml`` transform the Federal and NSW
formats into the same canonical model; ``silver`` flattens fragments into
hive-partitioned Parquet; ``runner`` parallelizes across house-days.
"""
