#!/bin/bash

# cluster
$SPARK_HOME/bin/spark-submit --master spark://ip-10-0-0-4:7077 --conf spark.yarn.executor.memoryOverhead=600 --executor-memory 6G --driver-memory 6G preprocess.py

#local
# $SPARK_HOME/bin/spark-submit --packages com.amazonaws:aws-java-sdk-pom:1.10.34,org.apache.hadoop:hadoop-aws:2.7.3 --jars /usr/lib/spark/lib/datanucleus-api-jdo-3.2.6.jar,/usr/lib/spark/lib/datanucleus-core-3.2.10.jar,/usr/lib/spark/lib/datanucleus-rdbms-3.2.9.jar preprocess.py