
# script to load boundary & point data from Postgres and export it to parquet format for later use in a Spark script

import logging
import os
import psycopg2
import sys

from datetime import datetime
from multiprocessing import cpu_count
from pyspark.sql import functions as f, types as t
from pyspark.sql import SparkSession

from geospark.register import upload_jars, GeoSparkRegistrator  # need to install geospark package
from geospark.utils import KryoSerializer, GeoSparkKryoRegistrator

# # REQUIRED FOR DEBUGGING IN IntelliJ/Pycharm ONLY - comment out if running from command line
# # set Conda environment vars for PySpark
# os.environ["JAVA_HOME"] = "/Library/Java/JavaVirtualMachines/adoptopenjdk-8.jdk/Contents/Home"
# os.environ["SPARK_HOME"] = "/Users/hugh.saalmans/spark-2.4.6-bin-hadoop2.7"
# os.environ["SPARK_LOCAL_IP"] = "127.0.0.1"
# os.environ["PYSPARK_PYTHON"] = "/Users/hugh.saalmans/opt/miniconda3/envs/geospark_env/bin/python"
# os.environ["PYSPARK_DRIVER_PYTHON"] = "/Users/hugh.saalmans/opt/miniconda3/envs/geospark_env/bin/python"
# os.environ["PYLIB"] = os.environ["SPARK_HOME"] + "/python/lib"


# get postgres parameters from local text file
# format per connection is:  server_name := HOST|hostname,DB|database,PORT|port_number,USER|username,PASS|password
def get_password(connection_name):
    passwords_file_path = os.path.join(os.environ["GIT_HOME"], "passwords.ini")

    if os.path.exists(passwords_file_path):
        passwords_file = open(passwords_file_path, 'r').read().splitlines()
        passwords_file = [i for i in passwords_file if len(i) != 0]  # remove empty lines
        passwords_file = [i for i in passwords_file if i[0] != "#"]  # remove comment lines

        params = dict()
        for ini in passwords_file:
            params[ini.split()[0].rstrip().lstrip()] = ini.split(':=')[1].rstrip().lstrip()

        return dict(item.split("|") for item in params[connection_name].split(","))


local_pg_settings = get_password("localhost_super")

# create postgres JDBC url
jdbc_url = "jdbc:postgresql://{HOST}:{PORT}/{DB}".format(**local_pg_settings)

# get connect string for psycopg2
local_pg_connect_string = "dbname={DB} host={HOST} port={PORT} user={USER} password={PASS}".format(**local_pg_settings)

# output path for gzipped parquet files
output_path = os.path.join(os.path.dirname(os.path.realpath(__file__)), "data")

# gnaf csv file
input_file_name = os.path.join(output_path, "gnaf.csv")


def main():
    start_time = datetime.now()

    # # copy gnaf tables to CSV
    # pg_conn = psycopg2.connect(local_pg_connect_string)
    # pg_cur = pg_conn.cursor()
    #
    # sql = """COPY (
    #              SELECT gid, gnaf_pid, street_locality_pid, locality_pid, alias_principal, primary_secondary, building_name,
    #                     lot_number, flat_number, level_number, number_first, number_last, street_name, street_type,
    #                     street_suffix, address, locality_name, postcode, state, locality_postcode, confidence,
    #                     legal_parcel_id, mb_2011_code, mb_2016_code, latitude, longitude, geocode_type, reliability
    #              FROM gnaf_202008.{}
    #          ) TO STDOUT WITH CSV"""
    #
    # # address principals
    # with open(os.path.join(output_path, "gnaf.csv"), 'w') as csv_file:
    #     pg_cur.copy_expert(sql.format("address_principals") + " HEADER", csv_file)
    #
    # # address aliases
    # with open(os.path.join(output_path, "gnaf.csv"), 'a') as csv_file:
    #     pg_cur.copy_expert(sql.format("address_aliases"), csv_file)
    #
    # pg_cur.close()
    # pg_conn.close()
    #
    # logger.info("\t - GNAF points exported to CSV: {}".format( datetime.now() - start_time))
    # start_time = datetime.now()

    # upload Sedona (geospark) JARs
    upload_jars()

    spark = (SparkSession
             .builder
             .master("local[*]")
             .appName("compass_iot_query")
             .config("spark.sql.session.timeZone", "UTC")
             .config("spark.sql.debug.maxToStringFields", 100)
             .config("spark.serializer", KryoSerializer.getName)
             .config("spark.kryo.registrator", GeoSparkKryoRegistrator.getName)
             .config("spark.cores.max", cpu_count())
             .config("spark.sql.adaptive.enabled", "true")
             .config("spark.driver.memory", "8g")
             .getOrCreate()
             )

    # Register Apache Sedona (geospark) UDTs and UDFs
    GeoSparkRegistrator.registerAll(spark)

    logger.info("\t - PySpark {} session initiated: {}".format(spark.sparkContext.version, datetime.now() - start_time))
    start_time = datetime.now()

    # load gnaf points
    df = spark.read \
        .option("header", True) \
        .option("inferSchema", True) \
        .csv(input_file_name)
    # df.printSchema()
    # df.show()

    # # manually assign field types (not needed here as inferSchema works)
    # df2 = (df.withColumn("gid", df.gid.cast(t.IntegerType()))
    #        .withColumn("confidence", df.confidence.cast(t.ShortType()))
    #        .withColumn("mb_2011_code", df.mb_2011_code.cast(t.LongType()))
    #        .withColumn("mb_2016_code", df.mb_2016_code.cast(t.LongType()))
    #        .withColumn("reliability", df.reliability.cast(t.ShortType()))
    #        .withColumn("longitude", df.longitude.cast(t.DoubleType()))
    #        .withColumn("latitude", df.latitude.cast(t.DoubleType()))
    #        )
    # # df2.printSchema()
    # # df2.show()

    # add point geometries and partition by longitude into 400-500k row partitions
    gnaf_df = df \
        .withColumn("geom", f.expr("ST_Point(longitude, latitude)")) \
        .repartitionByRange(32, "longitude")

    # check partition counts
    # gnaf_df.groupBy(f.spark_partition_id()).count().show()

    # write gnaf to gzipped parquet
    export_to_parquet(gnaf_df, "gnaf")

    # cleanup
    spark.stop()

    logger.info("\t - GNAF exported to gzipped parquet files: {}"
                .format(datetime.now() - start_time))


def get_dataframe_from_postgres(spark, sql):
    df = spark.read.format("jdbc") \
        .option("url", jdbc_url) \
        .option("query", sql) \
        .option("properties", local_pg_settings["USER"]) \
        .option("password", local_pg_settings["PASS"]) \
        .option("driver", "org.postgresql.Driver") \
        .load()
    return df

# .option("numPartitions", 32) \
# .option("partitionColumn", "gid") \


def export_to_parquet(df, name):
    df.write.option("compression", "gzip") \
        .mode("overwrite") \
        .parquet(os.path.join(output_path, name))


if __name__ == "__main__":
    full_start_time = datetime.now()

    # setup logging
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)

    # set Spark logging levels
    logging.getLogger("pyspark").setLevel(logging.ERROR)
    logging.getLogger("py4j").setLevel(logging.ERROR)

    # set logger
    log_file = os.path.abspath(__file__).replace(".py", ".log")
    logging.basicConfig(filename=log_file, level=logging.DEBUG, format="%(asctime)s %(message)s",
                        datefmt="%m/%d/%Y %I:%M:%S %p")

    # setup logger to write to screen as well as writing to log file
    # define a Handler which writes INFO messages or higher to the sys.stderr
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    # set a format which is simpler for console use
    formatter = logging.Formatter("%(name)-12s: %(levelname)-8s %(message)s")
    # tell the handler to use this format
    console.setFormatter(formatter)
    # add the handler to the root logger
    logging.getLogger("").addHandler(console)

    task_name = "Geospark testing"
    system_name = "mobility.ai"

    logger.info("{} started".format(task_name))
    logger.info("Running on Python {}".format(sys.version.replace("\n", " ")))

    main()

    time_taken = datetime.now() - full_start_time
    logger.info("{} finished : {}".format(task_name, time_taken))
    print()
