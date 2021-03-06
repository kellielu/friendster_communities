import sys
import os
import time
import json
from termcolor import colored

from pyspark.conf import SparkConf
from pyspark.context import SparkContext
from pyspark.sql import SQLContext
from pyspark.sql.functions import udf, col
import redis

from pyspark.sql.types import IntegerType, ArrayType

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/config")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/lib")
import config
import util
import min_hash
import locality_sensitive_hash


# Store question data
def store_lsh_redis(rdd):
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)
    for q in rdd:
        tags = q.tags.split("|")
        for tag in tags:
            q_json = json.dumps({"id": q.id, "title": q.title, "min_hash": q.min_hash, "lsh_hash": q.lsh_hash, "timestamp": q.creation_date})
            rdb.zadd("lsh:{0}".format(tag), q.view_count, q_json)
            rdb.sadd("lsh_keys", "lsh:{0}".format(tag))


# Computes MinHashes, LSHes for all in DataFrame
def compute_minhash_lsh(df, mh, lsh):
    calc_min_hash = udf(lambda x: list(map(lambda x: int(x), mh.calc_min_hash_signature(x))), ArrayType(IntegerType()))
    calc_lsh_hash = udf(lambda x: list(map(lambda x: int(x), lsh.find_lsh_buckets(x))), ArrayType(IntegerType()))

    df = df.withColumn("min_hash", calc_min_hash("text_body_stemmed"))
    df = df.withColumn("lsh_hash", calc_lsh_hash("min_hash"))

    df.foreachPartition(store_lsh_redis)


# Store duplicate candidates in Redis
def store_dup_cand_redis(tag, rdd):
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)
    for cand in rdd:
        cand_reformatted = (tag, cand.q1_id, cand.q1_title, cand.q2_id, cand.q2_title, cand.timestamp)
        # Store by time
        rdb.zadd("dup_cand", cand.mh_js, cand_reformatted)


# Compares LSH signatures, MinHash signature, and find duplicate candidates
def find_dup_cands_within_tags(mh, lsh):
    rdb = redis.StrictRedis(config.REDIS_SERVER, port=6379, db=0)

    # Fetch all tags from lsh_keys set
    for lsh_key in rdb.sscan_iter("lsh_keys", match="*", count=500):
        tag = lsh_key.replace("lsh:", "")
        tq_table_size = rdb.zcard("lsh:{0}".format(tag))
        if(tq_table_size >= config.DUP_QUESTION_MIN_TAG_SIZE):  # Ignore extremely small tags
            tq = rdb.zrangebyscore("lsh:{0}".format(tag), "-inf", "+inf", withscores=False)
            if config.LOG_DEBUG: print(colored("{0}: {1} question(s)".format(tag, len(tq)), "yellow"))
            tq_df = sql_context.read.json(sc.parallelize(tq))

            find_lsh_sim = udf(lambda x, y: lsh.common_bands_count(x, y), IntegerType())
            lsh_sim_df = tq_df.alias("q1").join(tq_df.alias("q2"), col("q1.id") < col("q2.id")).select(
                col("q1.id").alias("q1_id"),
                col("q2.id").alias("q2_id"),
                col("q1.min_hash").alias("q1_min_hash"),
                col("q2.min_hash").alias("q2_min_hash"),
                col("q1.title").alias("q1_title"),
                col("q2.title").alias("q2_title"),
                col("q1.timestamp").alias("timestamp"),
                find_lsh_sim("q1.lsh_hash", "q2.lsh_hash").alias("lsh_sim")
            ).sort("q1_id", "q2_id")

            # Duplicate candidates have a high enough LSH similarity count
            lsh_cand_df = lsh_sim_df.filter(lsh_sim_df.lsh_sim >= config.LSH_SIMILARITY_BAND_COUNT)

            # Compare MinHash jaccard similarity scores for duplicate candidates
            find_mh_js = udf(lambda x, y: mh.jaccard_sim_score(x, y))
            mh_cand_df = lsh_cand_df.withColumn("mh_js", find_mh_js(lsh_cand_df.q1_min_hash, lsh_cand_df.q2_min_hash))

            # Duplicate candidates need to have a high enough MinHash Jaccard similarity score
            dup_cand_df = mh_cand_df.filter(mh_cand_df.mh_js >= config.DUP_QUESTION_MIN_HASH_THRESHOLD)
            dup_cand_df.foreachPartition(lambda rdd: store_dup_cand_redis(tag, rdd))


def run_minhash_lsh():
    df = util.read_all_json_from_bucket(sql_context, config.S3_BUCKET_BATCH_PREPROCESSED)
    #  Create and save MinHash and LSH if not exist or load them from file
    if(not os.path.isfile(config.MIN_HASH_PICKLE) or not os.path.isfile(config.LSH_PICKLE)):
        mh = min_hash.MinHash(config.MIN_HASH_K_VALUE)
        lsh = locality_sensitive_hash.LSH(config.LSH_NUM_BANDS, config.LSH_BAND_WIDTH, config.LSH_NUM_BUCKETS)
        util.save_pickle_file(mh, config.MIN_HASH_PICKLE)
        util.save_pickle_file(lsh, config.LSH_PICKLE)
    else:
        mh = util.load_pickle_file(config.MIN_HASH_PICKLE)
        lsh = util.load_pickle_file(config.LSH_PICKLE)

    # Compute MinHash/LSH hashes for every question
    if (config.LOG_DEBUG): print(colored("[BATCH]: Calculating MinHash hashes and LSH hashes...", "green"))
    compute_minhash_lsh(df, mh, lsh)

    # Compute pairwise LSH similarities for questions within tags
    if (config.LOG_DEBUG): print(colored("[BATCH]: Fetching questions in same tag, comparing LSH and MinHash, uploading duplicate candidates back to Redis...", "cyan"))
    find_dup_cands_within_tags(mh, lsh)


def main():
    spark_conf = SparkConf().setAppName("Spark Custom MinHashLSH").set("spark.cores.max", "30")

    global sc
    sc = SparkContext(conf=spark_conf)
    sc.setLogLevel("ERROR")
    sc.addFile(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/lib/min_hash.py")
    sc.addFile(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/lib/locality_sensitive_hash.py")
    sc.addFile(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/lib/util.py")
    sc.addFile(os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/config/config.py")

    global sql_context
    sql_context = SQLContext(sc)

    start_time = time.time()
    run_minhash_lsh()
    end_time = time.time()
    print(colored("Spark Custom MinHashLSH run time (seconds): {0} seconds".format(end_time - start_time), "magenta"))


if(__name__ == "__main__"):
    main()
