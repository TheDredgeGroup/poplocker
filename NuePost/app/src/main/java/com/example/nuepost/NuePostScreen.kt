package com.example.nuepost

import android.content.Context
import android.view.MotionEvent
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.ExperimentalComposeUiApi
import androidx.compose.ui.Modifier
import androidx.compose.ui.input.pointer.pointerInteropFilter
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp

@OptIn(ExperimentalComposeUiApi::class)
@Composable
fun NuePostScreen(onMicDown: () -> Unit, onMicUp: () -> Unit) {
    val context = LocalContext.current
    val prefs = context.getSharedPreferences("nuepost", Context.MODE_PRIVATE)

    var url by remember { mutableStateOf(prefs.getString("url", "") ?: "") }
    var key by remember { mutableStateOf(prefs.getString("key", "text") ?: "text") }
    var showSettings by remember { mutableStateOf(false) }
    var isListening by remember { mutableStateOf(false) }
    var statusText by remember { mutableStateOf("Hold to speak") }

    if (showSettings) {
        AlertDialog(
            onDismissRequest = { showSettings = false },
            title = { Text("Settings") },
            text = {
                Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                    OutlinedTextField(
                        value = url,
                        onValueChange = { url = it },
                        label = { Text("Endpoint URL") },
                        modifier = Modifier.fillMaxWidth()
                    )
                    OutlinedTextField(
                        value = key,
                        onValueChange = { key = it },
                        label = { Text("JSON Key") },
                        modifier = Modifier.fillMaxWidth()
                    )
                }
            },
            confirmButton = {
                TextButton(onClick = {
                    prefs.edit().putString("url", url).putString("key", key).apply()
                    showSettings = false
                }) { Text("Save") }
            },
            dismissButton = {
                TextButton(onClick = { showSettings = false }) { Text("Cancel") }
            }
        )
    }

    Scaffold(
        topBar = {
            @OptIn(ExperimentalMaterial3Api::class)
            TopAppBar(
                title = { Text("NuePost") },
                actions = {
                    TextButton(onClick = { showSettings = true }) { Text("Settings") }
                }
            )
        }
    ) { paddingValues ->
        Box(
            modifier = Modifier
                .fillMaxSize()
                .padding(paddingValues),
            contentAlignment = Alignment.Center
        ) {
            Column(
                horizontalAlignment = Alignment.CenterHorizontally,
                verticalArrangement = Arrangement.Center
            ) {
                Text(statusText, fontSize = 18.sp)
                Spacer(modifier = Modifier.height(32.dp))
                Button(
                    onClick = {},
                    modifier = Modifier
                        .size(160.dp)
                        .pointerInteropFilter { event ->
                            when (event.action) {
                                MotionEvent.ACTION_DOWN -> {
                                    isListening = true
                                    statusText = "Listening..."
                                    onMicDown()
                                    true
                                }
                                MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                                    isListening = false
                                    statusText = "Hold to speak"
                                    onMicUp()
                                    true
                                }
                                else -> false
                            }
                        },
                    shape = CircleShape,
                    colors = ButtonDefaults.buttonColors(
                        containerColor = if (isListening)
                            MaterialTheme.colorScheme.error
                        else
                            MaterialTheme.colorScheme.primary
                    )
                ) {
                    Text("🎤", fontSize = 48.sp)
                }
            }
        }
    }
}