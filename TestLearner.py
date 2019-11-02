from lyft_dataset_sdk.lyftdataset import LyftDataset
from keras.models import Model
from keras.layers import Dense, Input, BatchNormalization, Layer, Concatenate, Conv3D, ZeroPadding3D, \
    Reshape, Permute, ZeroPadding2D, Conv2D, Conv2DTranspose
from keras.utils import plot_model
import keras.backend as tf_backend

import numpy as np
from pyquaternion import Quaternion
from matplotlib import pyplot as plt
from math import floor
import os
from tensorflow import SparseTensor

# constants
# size of voxel
voxelx = 0.5
voxely = 0.25
voxelz = 0.25

# Number of voxels in space that we care about
nx = int(100 / voxelx)
ny = int(100 / voxely)
nz = int(2 / voxelz)

# Limit of points per voxel to reduce size of data.
maxPoints = 35

# index of points in input tensor
pointIndex = -2


# load dataset
level5Data = LyftDataset(
    data_path='C:\\Users\\pmwws\\Documents\\ML project\\3d-object-detection-for-autonomous-vehicles',
    json_path='C:\\Users\\pmwws\\Documents\\ML project\\3d-object-detection-for-autonomous-vehicles\\train_data',
    verbose=True
)


# helper layer that transforms the (None, 250, 500, 10, 1, 6) into (None, 250, 500, 10, 35, 6) for concat
class RepeatLayer(Layer):
    def __init__(self, **kwargs):
        super(RepeatLayer, self).__init__(**kwargs)

    def compute_output_shape(self, inputShape):
        return inputShape[:pointIndex] + (maxPoints,) + inputShape[pointIndex + 1:]

    def call(self, inputs, **kwargs):
        return tf_backend.repeat_elements(inputs, maxPoints, pointIndex)


# special pooling layer for VFE block
class MaxPoolingVFELayer(Layer):
    def __init__(self, combine=False, **kwargs):
        super(MaxPoolingVFELayer, self).__init__(**kwargs)
        self.combineDim = combine

    def compute_output_shape(self, inputShape):
        if not self.combineDim:
            return inputShape[:pointIndex] + (1,) + inputShape[pointIndex + 1:]
        else:
            return inputShape[:pointIndex] + inputShape[pointIndex + 1:]

    def call(self, inputs, **kwargs):
        return tf_backend.max(inputs, axis=pointIndex, keepdims=not self.combineDim)


# Uses quaternions to rotate all points in a scene to match the location of the lidar sensor on the car.
def rotate_points(points, rotation, inverse=False):
    quaternion = Quaternion(rotation)
    if inverse:
        quaternion = quaternion.inverse
    return np.dot(quaternion.rotation_matrix, points.T).T


# Takes the sample dict and returns an array of n,3 with every point in the sample.
def combine_lidar_data(sample, dataDir):
    sensorTypes = ['LIDAR_TOP', 'LIDAR_FRONT_RIGHT', 'LIDAR_FRONT_LEFT']
    sensorFrameMetadata = [level5Data.get('sample_data', sample['data'][x]) for x in sensorTypes]
    allPoints = []
    for sensorFrame in sensorFrameMetadata:
        sensor = level5Data.get('calibrated_sensor', sensorFrame['calibrated_sensor_token'])
        # get points
        filePath = sensorFrame['filename'].replace('/', '\\')
        rawPoints = np.fromfile(os.path.join(dataDir, filePath), dtype=np.float32)

        # reshape to get each point (5 values, then drop last 2 since they are intensiy and always 1?)
        rawPoints = rawPoints.reshape(-1, 5)[:, :3]

        # need to rotate points per sensor, then translate to position of sensor before combining
        points = rotate_points(rawPoints, sensor['rotation'])
        points = points + np.array(sensor['translation'])
        allPoints.append(points)
    allPoints = np.concatenate(allPoints)

    return allPoints


# given a x,y,z, find coordinate of voxel it woul be in.
# Voxels are defined by their lower leftmost point
def get_voxel(point, xSize, ySize, zSize):
    x = floor(point[0] / xSize)
    y = floor(point[1] / ySize)
    z = floor(point[2] / zSize)
    return (x, y, z)


# takes an array of size n,3 (every lidar point in sample) and returns array of points to pass into VFE
# Returns array of size n,6
def VFE_preprocessing(points, xSize, ySize, zSize, sampleSize, maxVoxelX, maxVoxelY, maxVoxelZ):
    clusteredPoints = {}
    # Iterate through points and add them to voxels
    for idx, point in enumerate(points):
        # expecting n to be around 200,000. Could be bad. Average time on local machine is about ~15 sec
        key = get_voxel(point, xSize, ySize, zSize)
        if -maxVoxelX < key[0] and key[0] < maxVoxelX \
                and -maxVoxelY < key[1] and key[1] < maxVoxelY \
                and -maxVoxelZ < key[2] and key[2] < maxVoxelZ:
            if key in clusteredPoints:
                clusteredPoints[key].append(idx)
            else:
                clusteredPoints[key] = [idx]
    # Add voxels that are empty
    # for z in range(maxVoxelZ + 1):
    #     for y in range(-maxVoxelY, maxVoxelY + 1):
    #         for x in range(-maxVoxelX, maxVoxelX + 1):
    #             if (x, y, z) not in clusteredPoints:
    #                 clusteredPoints[(x, y, z)] = []
    # Sample points and fil the rest of the voxel if not full
    appendedPoints = {}
    for voxel in clusteredPoints:
        # sample points, then find center
        s = sampleSize if len(clusteredPoints[voxel]) > sampleSize else len(clusteredPoints[voxel])
        sampleIdx = np.random.choice(clusteredPoints[voxel], size=s, replace=False)
        # get points for this voxel
        currPoints = points[sampleIdx]
        centroid = np.mean(currPoints, axis=0)
        # subtract constant x, y, z values of centroid from each column. Use 0:1 to keep it as a 2D array
        centroidX = currPoints[:, 0:1] - centroid[0]
        centroidY = currPoints[:, 1:2] - centroid[1]
        centroidZ = currPoints[:, 2:3] - centroid[2]
        concat = np.hstack((currPoints, centroidX, centroidY, centroidZ))
        buffer = np.vstack((concat, np.zeros((sampleSize - s, 6))))
        appendedPoints[voxel] = buffer
    indices = []
    values = []
    for voxel in appendedPoints:
        for i in range(len(appendedPoints[voxel])):
            for j in range(len(appendedPoints[voxel][i])):
                indices.append((voxel[2],) + voxel[:2] + (i, j))
                values.append(appendedPoints[voxel][i][j])
    # return as z, x, y
    return SparseTensor(indices=indices, values=values, dense_shape=[maxVoxelZ, maxVoxelX*2, maxVoxelY*2, sampleSize,6])


def addVFELayer(layer, startNum, endNum):
    # FCN is linear, batch normalize, then relu
    actualEndNum = endNum // 2
    layer = addFCN(layer, startNum, actualEndNum)
    # now do the max pooling per
    pooling = MaxPoolingVFELayer()(layer)
    pooling = RepeatLayer()(pooling)
    out = Concatenate()([pooling, layer])
    return out


def addFCN(layer, startNum, endNum):
    layer = Dense(endNum)(layer)
    layer = BatchNormalization()(layer)
    layer = Dense(endNum, activation='relu')(layer)
    return layer


# Convolution middle layers.
# k = kernel size, s = stride size, p = padding to add.
def addConv3DLayer(layer, cin, cout, k, s, p):
    layer = ZeroPadding3D(padding=p)(layer)
    layer = Conv3D(cout, kernel_size=k, strides=s, padding='valid')(layer)
    layer = BatchNormalization()(layer)
    layer = Dense(layer.shape[-1], activation='relu')(layer)
    return layer


# Convolution2D layers for RPN
# k = kernel size, s = stride size, p = padding to add.
def addConv2DLayer(layer, cin, cout, k, s, p):
    layer = ZeroPadding2D(padding=p)(layer)
    layer = Conv2D(cout, kernel_size=k, strides=s)(layer)
    layer = BatchNormalization()(layer)
    layer = Dense(layer.shape[-1], activation='relu')(layer)
    return layer


# Modification to RPN. Do a single layer of step size 2, then do q convolutions of step size 1.
def addRPNConvLayer(layer, cin, cout, q):
    layer = addConv2DLayer(layer, cin, cout, 3, 2, 1)
    for i in range(q):
        layer = addConv2DLayer(layer, cout, cout, 3, 1, 1)
    return layer


def getRPNInputShape(layerShape):
    return layerShape[1:-2] + (layerShape[-2] * layerShape[-1],)


def createModel(nx, ny, nz, maxPoints):
    # Keras time
    os.environ[
        "PATH"] += os.pathsep + 'C:\\Program Files\\Graphviz\\bin'

    # Input is a tensor that separates each voxel. Empty voxels are all 0.
    # VFE layers
    inLayer = Input(shape=(nz, nx, ny, maxPoints, 6), name='InputVoxel')
    outLayer = addVFELayer(inLayer, 6, 32)
    outLayer = addVFELayer(outLayer, 32, 128)
    outLayer = addFCN(outLayer, 128, 128)
    # Convolution layers. Just use default convolution algorithm.
    outLayer = MaxPoolingVFELayer(combine=True)(outLayer)
    outLayer = addConv3DLayer(outLayer, 128, 64, 3, (2, 1, 1), (1, 1, 1))
    outLayer = addConv3DLayer(outLayer, 64, 64, 3, (1, 1, 1), (0, 1, 1))
    outLayer = addConv3DLayer(outLayer, 64, 64, 3, (2, 1, 1), (1, 1, 1))
    # RPN layer time
    # format data so we can run RPN on it and treat it like a 2D image.
    # after each rpbConvLayer, decompose and save for concat at end.
    outLayer = Permute((2, 3, 4, 1))(outLayer)
    outLayer = Reshape(getRPNInputShape(outLayer.shape))(outLayer)
    # block 1
    rpnConv = addRPNConvLayer(outLayer, 128, 128, 3)
    rpnConv1Out = Conv2DTranspose(256, strides=1, kernel_size=3, padding='same')(rpnConv)
    # block 2
    rpnConv = addRPNConvLayer(rpnConv, 128, 128, 5)
    rpnConv2Out = Conv2DTranspose(256, strides=2, kernel_size=2, padding='same')(rpnConv)
    # block 3
    rpnConv = addRPNConvLayer(rpnConv, 128, 256, 5)
    rpnConv3Out = Conv2DTranspose(256, strides=4, kernel_size=4, padding='same')(rpnConv)
    outLayer = Concatenate()([rpnConv1Out, rpnConv2Out, rpnConv3Out])
    probabilityLayer = Conv2D(2, kernel_size=1, strides=1, padding='same')(outLayer)
    regressionMap = Conv2D(14, kernel_size=1, strides=1, padding='same')(outLayer)
    model = Model(inputs=inLayer, outputs=[probabilityLayer, regressionMap])
    return model


def main():
    # Set constants
    dataDir = 'C:\\Users\\pmwws\\Documents\\ML project\\3d-object-detection-for-autonomous-vehicles'
    os.environ['TF_CPP_MIN_LOG_LEVEL'] = '2'

    # load first sample from first scene
    testScene = level5Data.scene[0]
    testSample = level5Data.get('sample', testScene['first_sample_token'])

    # level5Data.render_sample(testSample['token'])
    # Test view the resulting lidar data.
    # plt.figure(figsize=(12, 12))
    # plt.axis('equal')
    # plt.scatter(np.clip(testSampleLidarPoints[:,0],-50,50), np.clip(testSampleLidarPoints[:,1],-50,50), s=1, c='#000000')

    # import time
    # testSampleLidarPoints = combine_lidar_data(testSample, dataDir)
    # startTime = time.time()
    # testVFEPoints = VFE_preprocessing(testSampleLidarPoints, voxelx, voxely, voxelz, maxPoints, nx // 2, ny // 2, nz)
    # endTime = time.time()
    # print(endTime - startTime)
    # print(testVFEPoints.shape)

    model = createModel(nx, ny, nz, maxPoints)
    plot_model(model, show_shapes=True)
    model.compile('sgd',['mse', 'mse'])
    # model.fit(testVFEPoints, steps_per_epoch=1, epochs=1)
    # testVFEPoints = testVFEPoints.reshape((-1,) + testVFEPoints.shape)
    # p = model.predict(testVFEPoints, verbose=1, steps=1)
    # print(p)


if __name__ == '__main__':
    main()
